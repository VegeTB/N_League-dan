from astrbot.api.all import *
from astrbot.api.event.filter import command, on_decorating_result
from astrbot.api.message_components import At, Plain
import json
import os
import logging
import re
import time
import hashlib
from typing import Dict, List, Any, Tuple

logger = logging.getLogger("MahjongDanPlugin")

# 数据持久化路径
DATA_DIR = os.path.join("data", "plugins", "astrbot_mahjong_dan_plugin")
os.makedirs(DATA_DIR, exist_ok=True)
DAN_DATA_FILE = os.path.join(DATA_DIR, "dan_data.json")

# 老插件数据路径 (用于将昵称精准映射到唯一 uid)
MAHJONG_DATA_FILE = os.path.join("data", "plugins", "astrbot_mahjong_plugin", "mahjong_data.json")

# ==============================================================================
#  段位配置表 
# ==============================================================================
DAN_RANKS = [
    # 级位阶段: 保底机制，只升不降 (4位无惩罚)
    {"name": "新人", "target_pt": 20,   "init_pt": 0,    "is_dan": False, "level": 0},
    {"name": "9级",  "target_pt": 20,   "init_pt": 0,    "is_dan": False, "level": 0},
    {"name": "8级",  "target_pt": 20,   "init_pt": 0,    "is_dan": False, "level": 0},
    {"name": "7级",  "target_pt": 20,   "init_pt": 0,    "is_dan": False, "level": 0},
    {"name": "6级",  "target_pt": 40,   "init_pt": 0,    "is_dan": False, "level": 0},
    {"name": "5级",  "target_pt": 60,   "init_pt": 0,    "is_dan": False, "level": 0},
    {"name": "4级",  "target_pt": 80,   "init_pt": 0,    "is_dan": False, "level": 0},
    {"name": "3级",  "target_pt": 100,  "init_pt": 0,    "is_dan": False, "level": 0},
    {"name": "2级",  "target_pt": 100,  "init_pt": 0,    "is_dan": False, "level": 0},
    {"name": "1级",  "target_pt": 100,  "init_pt": 0,    "is_dan": False, "level": 0},
    
    # 段位阶段: 升段目标减半！初始 pt 位于正中 (跌破 0pt 降段)
    # 原版初段 800pt -> 现 400pt (初始 200pt)
    {"name": "初段", "target_pt": 400,  "init_pt": 200,  "is_dan": True,  "level": 1},
    {"name": "二段", "target_pt": 600,  "init_pt": 300,  "is_dan": True,  "level": 2},
    {"name": "三段", "target_pt": 800,  "init_pt": 400,  "is_dan": True,  "level": 3},
    {"name": "四段", "target_pt": 1000, "init_pt": 500,  "is_dan": True,  "level": 4},
    {"name": "五段", "target_pt": 1200, "init_pt": 600,  "is_dan": True,  "level": 5},
    {"name": "六段", "target_pt": 1400, "init_pt": 700,  "is_dan": True,  "level": 6},
    {"name": "七段", "target_pt": 1600, "init_pt": 800,  "is_dan": True,  "level": 7},
    {"name": "八段", "target_pt": 1800, "init_pt": 900,  "is_dan": True,  "level": 8},
    {"name": "九段", "target_pt": 2000, "init_pt": 1000, "is_dan": True,  "level": 9},
    {"name": "十段", "target_pt": 2200, "init_pt": 1100, "is_dan": True,  "level": 10},
    {"name": "SYC位", "target_pt": None, "init_pt": 0,   "is_dan": True,  "level": 11},
]

@register("mahjong_dan_plugin", "Vege", "天凤段位与Rate独立评级系统", "1.0.0")
class MahjongDanPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.dan_data = self._load_data()
        # 防止同一场对局因网络重传重复结算的哈希缓存 (记录最近20次结算签名)
        self._processed_matches = set()

    def _load_data(self) -> dict:
        if not os.path.exists(DAN_DATA_FILE):
            return {}
        try:
            with open(DAN_DATA_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"[DanPlugin] 数据加载失败: {e}")
            return {}

    def _save_data(self):
        try:
            with open(DAN_DATA_FILE, "w", encoding="utf-8") as f:
                json.dump(self.dan_data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"[DanPlugin] 数据保存失败: {e}")

    def _get_context_id(self, event: AstrMessageEvent) -> str:
        if hasattr(event, 'group_id') and event.group_id:
            return f"group_{event.group_id}"
        if hasattr(event, 'user_id') and event.user_id:
            return f"private_{event.user_id}"
        return "default_ctx"

    def _get_uid_by_name(self, ctx_id: str, name: str) -> str:
        """从老联赛数据中，通过昵称反查唯一 uid"""
        if os.path.exists(MAHJONG_DATA_FILE):
            try:
                with open(MAHJONG_DATA_FILE, "r", encoding="utf-8") as f:
                    mj_data = json.load(f)
                    group_data = mj_data.get(ctx_id, {})
                    for uid, udata in group_data.items():
                        if isinstance(udata, dict) and udata.get("name") == name:
                            return str(uid)
            except Exception as e:
                logger.debug(f"[DanPlugin] 反查uid异常: {e}")
        # 如果老数据找不到，使用 name 作为备用标识符
        return f"name_{name}"

    def _init_user_if_absent(self, ctx_data: dict, uid: str, name: str) -> dict:
        """初始化一个新人的天凤数据档案"""
        return ctx_data.setdefault(uid, {
            "name": name,
            "rate": 1500.0,
            "max_rate": 1500.0,
            "rank_idx": 0,        # 初始为新人 (index 0)
            "max_rank_idx": 0,
            "pt": 0,
            "matches": 0,
            "ranks": [0, 0, 0, 0] # 1~4位次数
        })

    # ==============================================================================
    # 🎯 核心黑科技：对局战报拦截与段位计算
    # ==============================================================================
    @on_decorating_result()
    async def on_decorating_result(self, event: AstrMessageEvent):
        """监听老插件发送的对局结算公告，无缝注入天凤段位计算"""
        result = event.get_result()
        if not result:
            return

        text = result.get_plain_text()
        if not text:
            return

        # 过滤规则：只捕获常规联赛的正式对局！
        # 排除 A规、活动规，确保天凤系统的竞技严肃性
        if ("对局结束" not in text and "本局结算" not in text):
            return
        if ("🅰️" in text or "A规" in text or "🃏" in text or "活动" in text):
            return

        # 解析四位玩家结算信息：
        # 兼容匹配诸如: 🥇 玩家A: 35000 (+40.0pt) 或 1位 玩家A: 35000 (+40.0pt)
        pattern = r"(?:[🥇🥈🥉💀]|\d+位)\s*([^:\n\r]+?):\s*(-?\d+)\s*\(([-+]?\d+(?:\.\d+)?pt)\)"
        matches = re.findall(pattern, text)

        if len(matches) != 4:
            return

        # 防重复结算签名校验 (MD5哈希)
        match_signature = hashlib.md5(text.encode("utf-8")).hexdigest()
        if match_signature in self._processed_matches:
            return
        self._processed_matches.add(match_signature)
        if len(self._processed_matches) > 50:
            self._processed_matches.pop()

        ctx_id = self._get_context_id(event)
        ctx_data = self.dan_data.setdefault(ctx_id, {})

        # 整理选手数据 (保持排位从高到低)
        player_scores = [] # [(uid, name, score)]
        for rank_idx, (name, score_str, _) in enumerate(matches):
            clean_name = name.strip()
            uid = self._get_uid_by_name(ctx_id, clean_name)
            score = int(score_str)
            user_stat = self._init_user_if_absent(ctx_data, uid, clean_name)
            user_stat["name"] = clean_name # 保持昵称最新
            player_scores.append((uid, clean_name, score))

        # --- 1. 计算 Rate (R值) ---
        # 桌平均 R (低于1500按1500计算)
        current_rates = [ctx_data[uid]["rate"] for uid, _, _ in player_scores]
        r_avg = sum(current_rates) / 4.0
        r_calc_avg = max(r_avg, 1500.0)

        # 顺位基础 R 变动值
        BASE_R = [30.0, 10.0, -10.0, -30.0]

        # --- 2. 考虑同分平分逻辑处理 R 和 pt ---
        settle_results = []
        events_notice = []

        i = 0
        while i < len(player_scores):
            j = i + 1
            while j < len(player_scores) and player_scores[j][2] == player_scores[i][2]:
                j += 1
            
            # 同分平均顺位 R 基础值
            avg_base_r = sum(BASE_R[i:j]) / (j - i)

            for k in range(i, j):
                uid, name, score = player_scores[k]
                user_stat = ctx_data[uid]
                
                # --- Rate 计算 ---
                c_factor = 1.0 - 0.002 * user_stat["matches"] if user_stat["matches"] < 400 else 0.2
                c_factor = max(c_factor, 0.2)
                correction = (r_calc_avg - user_stat["rate"]) / 40.0
                delta_r = round(c_factor * (avg_base_r + correction), 2)
                
                new_rate = round(user_stat["rate"] + delta_r, 2)
                user_stat["rate"] = new_rate
                user_stat["max_rate"] = max(user_stat.get("max_rate", 1500.0), new_rate)

                # --- 段位 pt 计算 ---
                cur_rank = DAN_RANKS[user_stat["rank_idx"]]
                
                # 计算该顺位的理论 pt
                def get_pt_for_rank(pos: int, is_dan: bool, level: int) -> float:
                    if not is_dan:
                        # 级位阶段: 1位+30, 2位+15, 3位0, 4位0
                        return [30.0, 15.0, 0.0, 0.0][pos]
                    else:
                        # 段位阶段: 1位+60, 2位+30, 3位0, 4位重罚
                        penalty = -15.0 * (level + 2)
                        return [60.0, 30.0, 0.0, penalty][pos]

                # 处理同分情况下的 pt 平分
                span_pts = [get_pt_for_rank(pos, cur_rank["is_dan"], cur_rank["level"]) for pos in range(i, j)]
                delta_pt = int(round(sum(span_pts) / len(span_pts)))

                old_rank_idx = user_stat["rank_idx"]
                old_pt = user_stat["pt"]
                user_stat["pt"] += delta_pt
                user_stat["matches"] += 1
                user_stat["ranks"][i] += 1 # 计入顺位

                # --- 升段与降段判定 ---
                rank_changed_msg = None
                
                # 升段判定
                if user_stat["rank_idx"] < len(DAN_RANKS) - 1:
                    target = DAN_RANKS[user_stat["rank_idx"]]["target_pt"]
                    if target is not None and user_stat["pt"] >= target:
                        user_stat["rank_idx"] += 1
                        new_rank = DAN_RANKS[user_stat["rank_idx"]]
                        user_stat["pt"] = new_rank["init_pt"]
                        user_stat["max_rank_idx"] = max(user_stat.get("max_rank_idx", 0), user_stat["rank_idx"])
                        rank_changed_msg = f"🎊 恭喜【{name}】升段至 【{new_rank['name']}】！交！"

                # 降段判定 (仅在初段及以上生效)
                if user_stat["pt"] < 0:
                    if cur_rank["is_dan"]:
                        user_stat["rank_idx"] -= 1
                        demoted_rank = DAN_RANKS[user_stat["rank_idx"]]
                        # 降级赋予上一级初始基准分
                        user_stat["pt"] = demoted_rank["init_pt"] if demoted_rank["is_dan"] else int(demoted_rank["target_pt"] * 0.75)
                        rank_changed_msg = f"📉 悲！【{name}】降段为 【{demoted_rank['name']}】！捞了捞了！"
                    else:
                        # 级位保底清零
                        user_stat["pt"] = 0

                if rank_changed_msg:
                    events_notice.append(rank_changed_msg)

                # 记录该玩家的显示日志
                target_pt_str = f"/{DAN_RANKS[user_stat['rank_idx']]['target_pt']}pt" if DAN_RANKS[user_stat['rank_idx']]['target_pt'] else ""
                r_sym = f"+{delta_r:.2f}" if delta_r > 0 else f"{delta_r:.2f}"
                pt_sym = f"+{delta_pt}" if delta_pt > 0 else f"{delta_pt}"
                
                settle_results.append(
                    f"{['🥇','🥈','🥉','💀'][k]} {name}: "
                    f"R{new_rate:.2f} ({r_sym}) | "
                    f"{DAN_RANKS[user_stat['rank_idx']]['name']} {user_stat['pt']}{target_pt_str} ({pt_sym})"
                )
            i = j

        self._save_data()

        # # --- 3. 构造天凤战报并优雅附加到原结算消息尾部 ---
        # dan_report_lines = [
        #     "",
        #     "------------------------",
        #     "🀄️ 【段位结算】"
        # ]
        # dan_report_lines.extend(settle_results)
        # if events_notice:
        #     dan_report_lines.append("")
        #     dan_report_lines.extend(events_notice)

        # dan_report_text = "\n".join(dan_report_lines)

        # # 直接将段位战报并入老插件的消息体中，单条消息同时呈现联赛与段位数据！
        # if hasattr(result, "chain") and isinstance(result.chain, list):
        #     result.chain.append(Plain(dan_report_text))

    # ==============================================================================
    # 📊 独立查询指令体系
    # ==============================================================================

    def _render_bar(self, pt: int, target: int, length: int = 10) -> str:
        """渲染高颜值段位进度条"""
        if not target or target <= 0:
            return "MAX"
        ratio = min(max(pt / target, 0.0), 1.0)
        filled = int(ratio * length)
        return "█" * filled + "░" * (length - filled)

    @command("mj_dan_stats", alias=["段位", "我的段位", "dan", "rate"])
    async def show_my_dan(self, event: AstrMessageEvent):
        """
        查询个人或他人的天凤段位档案
        用法: /段位  或  /段位 @选手
        """
        ctx_id = self._get_context_id(event)
        ctx_data = self.dan_data.get(ctx_id, {})

        target_uid = event.get_sender_id()
        target_name = event.get_sender_name()

        for comp in event.get_messages():
            if isinstance(comp, At):
                target_uid = str(comp.qq)
                if target_uid in ctx_data:
                    target_name = ctx_data[target_uid]["name"]
                else:
                    target_name = f"用户{target_uid}"
                break

        if target_uid not in ctx_data:
            yield event.plain_result(f"⚠️ 未找到 {target_name} 的对战记录。")
            return

        user = ctx_data[target_uid]
        total_m = user["matches"]
        if total_m == 0:
            yield event.plain_result(f"⚠️ {user['name']} 尚未完成过任何有效对局。")
            return

        rank_info = DAN_RANKS[user["rank_idx"]]
        max_rank_info = DAN_RANKS[user.get("max_rank_idx", user["rank_idx"])]
        
        # 进度条
        target = rank_info["target_pt"]
        bar_str = ""
        pct_str = ""
        if target:
            bar = self._render_bar(user["pt"], target)
            pct = (user["pt"] / target) * 100
            bar_str = f"\n  `{bar}` {user['pt']}/{target} pt ({pct:.1f}%)"
        else:
            bar_str = "\n  `██████████` (已通关！)"

        ranks = user["ranks"]
        rates = [f"{r / total_m * 100:.1f}%" for r in ranks]
        avg_rank = sum((i + 1) * count for i, count in enumerate(ranks)) / total_m
        avoid_4 = (sum(ranks[:3]) / total_m) * 100

        msg = [
            f"🥋 **{user['name']} 的段位档案**",
            f"========================",
            f"🎖️ 当前段位: **{rank_info['name']}**{bar_str}",
            f"📈 Rate: **R{user['rate']:.2f}** (最高: R{user.get('max_rate', user['rate']):.2f})",
            f"👑 历史最高: {max_rank_info['name']}",
            f"",
            f"📊 生涯统计 (共 {total_m} 场)",
            f"🥇一位率: {rates[0]} ({ranks[0]}回)",
            f"🥈二位率: {rates[1]} ({ranks[1]}回)",
            f"🥉三位率: {rates[2]} ({ranks[2]}回)",
            f"💀四位率: {rates[3]} ({ranks[3]}回)",
            f"📐 平均顺位: {avg_rank:.2f} | 🛡️ 避四率: {avoid_4:.1f}%",
        ]
        yield event.plain_result("\n".join(msg))

    @command("mj_r_rank", alias=["r榜", "R榜", "rate榜", "Rate榜"])
    async def show_r_rank(self, event: AstrMessageEvent):
        """Rate 战斗力排行榜"""
        ctx_id = self._get_context_id(event)
        ctx_data = self.dan_data.get(ctx_id, {})
        if not ctx_data:
            yield event.plain_result("⚠️ 暂无段位记录。")
            return

        users = list(ctx_data.values())
        users = [u for u in users if u["matches"] > 0]
        users.sort(key=lambda x: x["rate"], reverse=True)

        msg = ["⚡️ **【N_League Rate 排行榜】** ⚡️", "========================"]
        for i, u in enumerate(users):
            rank_name = DAN_RANKS[u["rank_idx"]]["name"]
            msg.append(f" {i+1}. {u['name']} — R{u['rate']:.2f} [{rank_name}] ({u['matches']}战)")
        yield event.plain_result("\n".join(msg))

    @command("mj_dan_rank", alias=["段位榜", "天凤榜", "天凤段位榜"])
    async def show_dan_rank(self, event: AstrMessageEvent):
        """段位阶梯排行榜"""
        ctx_id = self._get_context_id(event)
        ctx_data = self.dan_data.get(ctx_id, {})
        if not ctx_data:
            yield event.plain_result("⚠️ 暂无段位记录。")
            return

        users = list(ctx_data.values())
        users = [u for u in users if u["matches"] > 0]
        # 排序逻辑: 段位级别由高到低 -> 当前pt由高到低 -> Rate由高到低
        users.sort(key=lambda x: (x["rank_idx"], x["pt"], x["rate"]), reverse=True)

        msg = ["🏆 【段位排行】 🏆", "========================"]
        for i, u in enumerate(users):
            rank_name = DAN_RANKS[u["rank_idx"]]["name"]
            target = DAN_RANKS[u["rank_idx"]]["target_pt"]
            pt_desc = f"{u['pt']}/{target}pt" if target else f"{u['pt']}pt"
            msg.append(f" {i+1}. {u['name']} — {rank_name} ({pt_desc}) | R{u['rate']:.2f}")
        yield event.plain_result("\n".join(msg))

    @command("mj_dan_reset", alias=["重置段位", "段位重置"])
    async def reset_dan_data(self, event: AstrMessageEvent):
        """[管理员] 重置当前群的天凤段位和Rate数据"""
        ctx_id = self._get_context_id(event)
        if ctx_id in self.dan_data:
            self.dan_data[ctx_id] = {}
            self._save_data()
            yield event.plain_result("🔄 本群段位和 Rate 数据已全部重置！")
        else:
            yield event.plain_result("⚠️ 当前无段位数据。")
