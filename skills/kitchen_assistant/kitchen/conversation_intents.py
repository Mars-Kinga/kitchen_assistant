from __future__ import annotations

import re


_SEPARATORS = re.compile(r"[，,。！？!?、；;~～\s]+")
_QUESTION_MARKERS = ("吗", "么", "嘛", "怎么", "如何", "能不能", "可不可以", "要不要")
_NEGATIVE_MARKERS = (
    "不好", "不行", "不可以", "不要", "不用", "别", "取消", "还没", "没有", "不是",
    "先不", "不开始", "不确认", "换一个", "换一批",
)
_AFFIRMATIVE_EXACT = {
    "好", "好的", "好啊", "好呀", "好哒", "好嘞", "行", "行啊", "可以", "可以的",
    "没问题", "没毛病", "确认", "确认了", "确定", "确定了", "对", "对的", "是", "是的",
    "嗯", "嗯嗯", "嗯哼", "ok", "okay", "开始", "开始吧", "开做", "开做吧",
    "就这个", "就它", "选这个", "按这个", "按这个做", "照这个做",
}
_AFFIRMATIVE_PHRASES = (
    "就这个", "就按这个", "就选这个", "就这么做", "可以开始", "现在开始", "那就开始", "确认开始",
)
_RETRY_EXACT = {"重试", "再试一次", "重新试", "重新生成", "再生成一次"}
_GRATITUDE_EXACT = {"谢谢", "谢谢你", "多谢", "感谢", "辛苦了"}
_STEP_ACKNOWLEDGMENT_EXACT = {"好", "好的", "好吧", "行", "知道了", "明白了", "收到"}


def compact_command(text: str) -> str:
    return _SEPARATORS.sub("", str(text or "").strip().lower())


def is_affirmative(text: str) -> bool:
    """识别对当前明确提问的肯定答复，避免“好像”“可以吗”等误触发。"""
    compact = compact_command(text)
    if not compact or any(marker in compact for marker in _NEGATIVE_MARKERS):
        return False
    if any(marker in compact for marker in _QUESTION_MARKERS):
        return False
    return compact in _AFFIRMATIVE_EXACT or any(
        phrase in compact for phrase in _AFFIRMATIVE_PHRASES
    )


def is_recipe_confirmation(text: str) -> bool:
    compact = compact_command(text)
    return is_affirmative(compact) or compact in _RETRY_EXACT


def is_negative(text: str) -> bool:
    compact = compact_command(text)
    return bool(compact) and any(marker in compact for marker in _NEGATIVE_MARKERS)


def is_gratitude(text: str) -> bool:
    return compact_command(text) in _GRATITUDE_EXACT


def is_step_acknowledgment(text: str) -> bool:
    return compact_command(text) in _STEP_ACKNOWLEDGMENT_EXACT
