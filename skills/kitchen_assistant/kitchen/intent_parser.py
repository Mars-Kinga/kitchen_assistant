from __future__ import annotations

import re


_SERVINGS = {
    "一个人": 1, "一人": 1, "1 人": 1, "1人": 1, "1个人": 1,
    "两个人": 2, "二个人": 2, "两人": 2, "二人": 2, "2 人": 2, "2人": 2, "2个人": 2,
    "三个人": 3, "三人": 3, "3 人": 3, "3人": 3, "3个人": 3,
}
_SERVING_CHOICES = {
    "1": 1, "一": 1, "一个": 1, "一位": 1, "1个": 1, "1位": 1,
    "2": 2, "二": 2, "两": 2, "两个": 2, "二个": 2, "两位": 2, "2个": 2, "2位": 2,
    "3": 3, "三": 3, "三个": 3, "三位": 3, "3个": 3, "3位": 3,
}
_LOW_SALT = ("少盐", "清淡", "淡一点")
_NORMAL = ("正常口味", "正常就好", "正常", "正常的")
_CN_NUMBERS = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def extract_servings(text: str) -> int | None:
    compact = text.replace(" ", "")
    for phrase, value in _SERVINGS.items():
        if phrase.replace(" ", "") in compact:
            return value
    match = re.search(r"([123])\s*(?:个)?\s*人", text)
    if match:
        return int(match.group(1))
    chinese_match = re.search(r"([一二两三])\s*(?:个)?\s*人", text)
    return {"一": 1, "二": 2, "两": 2, "三": 3}.get(chinese_match.group(1)) if chinese_match else None


def extract_serving_choice(text: str) -> int | None:
    """Interpret a terse 1/2/3 response only after asking for servings."""
    return _SERVING_CHOICES.get(text.replace(" ", "").strip())


def extract_flavor(text: str) -> str | None:
    if any(word in text for word in _LOW_SALT):
        return "少盐"
    if any(word in text for word in _NORMAL):
        return "正常口味"
    return None


def extract_timer_seconds(text: str) -> int | None:
    if "计时" not in text and "定时" not in text:
        return None
    match = re.search(r"(\d+)\s*(秒|分钟|分)", text)
    if match:
        value = int(match.group(1))
        return value * 60 if match.group(2) in {"分钟", "分"} else value
    match = re.search(r"([一二两三四五六七八九十])\s*(秒|分钟|分)", text)
    if match:
        value = _CN_NUMBERS[match.group(1)]
        return value * 60 if match.group(2) in {"分钟", "分"} else value
    return None
