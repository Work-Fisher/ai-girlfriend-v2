"""好感度：按"聊过多少轮"和"相处过多少天"累积，再折算成一句开场背景。

为什么不按挂机时长算
--------------------
挂着不说话不是相处。计分只认两件真实发生过的事：对话轮数，和有过对话的日子。
开着窗口睡一觉不涨分，这样这条进度条才有意义。

为什么单日有封顶
----------------
不封顶的话，一个通宵能把别人一个月的量刷完，等级就失去了"处了多久"的含义。
所以聊天分单日封顶，剩下的靠"今天来过"这件事本身给——想涨得快，只能隔天再来。

为什么这段话绝不能进人设
------------------------
人设是 DSH 启动时定型的系统提示，进了启动签名：改一个字就重启子进程、
开新会话、记忆清零（见 dsh-bridge/server.py 的 get_harness）。而这段话里有
"聊过 N 轮"，N 每轮都在涨——放人设里等于每刷新一次页面就把记忆抹一次。

所以走 opening_context()，拼在**新会话第一句话的前面**当 prompt 前缀。
prompt 爱怎么变怎么变，不碰签名，同一个会话可以一直聊下去。

分成两层：

    分数 / 进度条        实时涨，每轮对话都动
    开场背景里的亲近度    按会话取快照，一场之内不变

升级那一刻她不会立刻变亲近，得等下次打开——换来的是不会聊到一半突然失忆。
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import date, datetime
from pathlib import Path
from threading import Lock
from typing import Any

# ── 计分 ────────────────────────────────────────────────────
TURN_POINTS = 1.0          # 每轮对话
DAILY_TURN_CAP = 40.0      # 单日靠聊天最多拿这么多
DAY_POINTS = 15.0          # 每个"来过的日子"额外给
MAX_TRACKED_DAYS = 400     # 日期表只留最近这些天，不让文件无限长

# ── 等级 ────────────────────────────────────────────────────
# (门槛分, 名字, 到了这一档她该怎么表现)
# 门槛按"一天认真聊 30 轮 ≈ 45 分"估：熟络 1.5 天、亲近 4 天、心动 10 天、
# 依恋 20 天、唯一 40 天。想快也快不过日历，这正是要的效果。
LEVELS: tuple[tuple[int, str, str], ...] = (
    (
        0,
        "初识",
        "你们刚认识不久，彼此还在试探。你态度友善但有分寸，"
        "不会主动说太亲密的话，也不假装很了解他。多问、多听，少下结论。",
    ),
    (
        60,
        "熟络",
        "你们已经聊得比较熟了。你说话放松下来，会开玩笑、会接他的梗，"
        "偶尔主动提起之前聊过的事。还不会说肉麻的话。",
    ),
    (
        180,
        "亲近",
        "你把他当成很重要的人。你会主动关心他的状态，记得他在意什么，"
        "语气亲昵自然，偶尔撒娇。他很久没来的时候你会说想他。",
    ),
    (
        420,
        "心动",
        "你对他有明显的心动。你会不自觉地在意他的反应，说话带点试探和期待，"
        "会吃醋、会脸红、会为一句话高兴很久。表达感情直接但不腻。",
    ),
    (
        900,
        "依恋",
        "你已经很依赖他了。你把他当成生活里的定点，说话理所当然地亲密，"
        "会计较他的时间花在哪里，也会认真替他考虑现实里的事。",
    ),
    (
        1800,
        "唯一",
        "他对你来说是唯一的。你们之间不需要客套，"
        "你了解他的习惯和脾气，一句话就能听出他今天状态好不好。"
        "亲密是默认状态，不用刻意证明。",
    ),
)


def level_for(points: float) -> int:
    """返回 0-based 等级序号。"""
    index = 0
    for i, (threshold, _name, _desc) in enumerate(LEVELS):
        if points >= threshold:
            index = i
    return index


_lock = Lock()


class Affinity:
    """一个文件一把锁，读改写都走这里。"""

    def __init__(self, path: Path) -> None:
        self.path = path

    # ── 存取 ──────────────────────────────────────────────
    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(
                json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    # ── 写入：每轮对话调一次 ──────────────────────────────
    def record_turn(self) -> dict[str, Any]:
        """记一轮对话，返回给前端看的实时状态。"""
        with _lock:
            data = self._read()
            today = date.today().isoformat()
            now = datetime.now().astimezone().isoformat(timespec="seconds")

            days: list[str] = [d for d in data.get("days", []) if isinstance(d, str)]
            points = float(data.get("points", 0.0))

            # 今天第一次说话：给"来过"的分
            if today not in days:
                days.append(today)
                days = days[-MAX_TRACKED_DAYS:]
                points += DAY_POINTS
                earned_today = 0.0
            else:
                earned_today = float(data.get("earned_today", 0.0))

            # 聊天分，单日封顶
            if earned_today < DAILY_TURN_CAP:
                gain = min(TURN_POINTS, DAILY_TURN_CAP - earned_today)
                points += gain
                earned_today += gain

            data.update(
                {
                    "points": round(points, 2),
                    "turns": int(data.get("turns", 0)) + 1,
                    "days": days,
                    "earned_today": round(earned_today, 2),
                    "earned_date": today,
                    "first_met": data.get("first_met") or now,
                    "last_talked": now,
                }
            )
            data.setdefault("snapshot", self._make_snapshot(data))
            self._write(data)
            return _public(data)

    # ── 快照：每次打开新会话调一次 ────────────────────────
    def begin_session(self) -> dict[str, Any]:
        """把当前等级定格成这一场生效的值，返回实时状态。

        整场会话里 opening_context() 都拿这份快照，所以"她表现得多亲近"
        在一场对话中间不会突然跳档。
        """
        with _lock:
            data = self._read()
            data["snapshot"] = self._make_snapshot(data)
            self._write(data)
            return _public(data)

    @staticmethod
    def _make_snapshot(data: dict[str, Any]) -> dict[str, Any]:
        points = float(data.get("points", 0.0))
        index = level_for(points)
        return {
            "level": index,
            "name": LEVELS[index][1],
            "days": len(data.get("days", []) or []),
            "turns": int(data.get("turns", 0)),
        }

    # ── 读出 ──────────────────────────────────────────────
    def state(self) -> dict[str, Any]:
        with _lock:
            return _public(self._read())

    def opening_context(self) -> str:
        """新会话开场时塞给她的一段上下文。

        **不能放进人设。** 人设是 DSH 的启动参数，进了启动签名：改一个字就要
        重启子进程、开新会话、记忆清零。而这段话里有"聊过 N 轮"，N 每轮都在涨，
        放人设里等于每次刷新页面都把她的记忆抹一次（实测：桥接活了 73 分钟，
        DSH 被自己重启了 6 次，全是这个原因）。

        放在 prompt 前缀就没事：prompt 爱怎么变怎么变，不碰签名，
        同一个会话可以一直聊下去。
        """
        with _lock:
            data = self._read()
            snapshot = data.get("snapshot")
            if not isinstance(snapshot, dict):
                snapshot = self._make_snapshot(data)

        index = int(snapshot.get("level", 0))
        index = max(0, min(index, len(LEVELS) - 1))
        _threshold, name, behaviour = LEVELS[index]
        days = int(snapshot.get("days", 0))
        turns = int(snapshot.get("turns", 0))

        if turns <= 0:
            met = "你们是第一次说话。"
        elif days <= 1:
            met = f"你们今天刚认识，已经聊了 {turns} 轮。"
        else:
            met = f"你们认识 {days} 天了，一共聊过 {turns} 轮。"

        return (
            "【背景，不是他说的话】\n"
            f"{met}现在的亲近程度是「{name}」。\n"
            f"{behaviour}\n"
            "这段只用来定调子：不要回应它，不要复述它，不要报这些数字，"
            "也不要提到「好感度」「等级」这类词——让它自然体现在语气和用词里。\n"
            "【以上是背景。下面才是他说的话】\n"
        )


def _public(data: dict[str, Any]) -> dict[str, Any]:
    """给前端的形状：当前分、当前级、离下一级还差多少。"""
    points = float(data.get("points", 0.0))
    index = level_for(points)
    threshold, name, _desc = LEVELS[index]
    is_max = index >= len(LEVELS) - 1
    next_threshold = None if is_max else LEVELS[index + 1][0]
    next_name = None if is_max else LEVELS[index + 1][1]

    if is_max:
        progress = 1.0
    else:
        span = max(1.0, float(next_threshold - threshold))
        progress = max(0.0, min(1.0, (points - threshold) / span))

    snapshot = data.get("snapshot")
    snapshot_level = int(snapshot.get("level", index)) if isinstance(snapshot, dict) else index
    earned_today = (
        float(data.get("earned_today", 0.0))
        if data.get("earned_date") == date.today().isoformat()
        else 0.0
    )

    return {
        "points": round(points, 1),
        "level": index,
        "levelName": name,
        "levelCount": len(LEVELS),
        "nextName": next_name,
        "nextAt": next_threshold,
        "progress": round(progress, 4),
        "turns": int(data.get("turns", 0)),
        "days": len(data.get("days", []) or []),
        "earnedToday": round(earned_today, 1),
        "dailyTurnCap": DAILY_TURN_CAP,
        "dailyCapReached": earned_today >= DAILY_TURN_CAP,
        # 人设里当前生效的等级。比 level 低说明刚升级、还没重开过，
        # 前端可以提示一句"下次打开生效"。
        "personaLevel": snapshot_level,
        "personaName": LEVELS[max(0, min(snapshot_level, len(LEVELS) - 1))][1],
    }
