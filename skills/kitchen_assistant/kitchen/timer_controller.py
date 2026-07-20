from __future__ import annotations

from dataclasses import dataclass

from .intent_parser import is_likely_timer_start
from .parallel_prep import is_waiting_prep_instruction


@dataclass
class Timer:
    deadline: float
    seconds: int
    label: str = "烹饪"
    step_index: int | None = None
    paused_remaining_seconds: int | None = None


def format_seconds(seconds: int) -> str:
    return f"{seconds // 60} 分钟" if seconds >= 60 and seconds % 60 == 0 else f"{seconds} 秒"


def remaining_seconds(timer: Timer | None, now: float) -> int | None:
    if timer is None:
        return None
    if timer.paused_remaining_seconds is not None:
        return timer.paused_remaining_seconds
    return max(0, int(timer.deadline - now + 0.999))


def step_timer_hint(step: dict) -> str | None:
    seconds = step.get("duration_seconds")
    if not isinstance(seconds, (int, float)) or seconds <= 0:
        return None
    instruction = str(step.get("instruction", ""))
    duration = format_seconds(int(seconds))
    if is_waiting_prep_instruction(instruction):
        return f"准备计时 {duration}；开始腌制/浸泡后说“开始了”或“给我计时”，我就开始计时。"
    heat_actions = ("预热", "煮", "炖", "焖", "煎", "烤", "蒸", "炸", "焯", "炒", "收汁", "加热")
    if not any(word in instruction for word in heat_actions):
        return None
    return f"准备计时 {duration}；食材下锅后说“下锅了”或“开始”，我就开始计时。"


def signals_step_timer_start(text: str, step: dict) -> bool:
    compact = text.replace(" ", "")
    if any(marker in compact for marker in ("吗", "？", "?")) or not step_timer_hint(step):
        return False
    if is_likely_timer_start(text):
        return True
    action_markers = (
        "下锅", "倒入", "放入", "入锅", "开始翻炒", "开始腌制", "腌上了", "已经开始",
        "开始了", "开始啦", "开始喽", "开炒了", "开煎了", "现在开始", "可以计时",
    )
    timer_markers = (
        "计时", "倒计时", "给我计时", "帮我计时", "开始计时", "开始倒计时",
        "帮忙计时", "记一下时间", "计一下时",
    )
    return any(marker in compact for marker in action_markers + timer_markers)
