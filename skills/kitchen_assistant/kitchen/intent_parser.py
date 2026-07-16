from __future__ import annotations

import re
from difflib import SequenceMatcher


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
_SPOKEN_NOISE = re.compile(r"[，,。！？!?、~～\s]+")
_QUESTION_MARKERS = ("吗", "？", "?", "怎么", "如何", "什么时候", "要不要", "能不能", "哪一步", "做到哪", "到哪", "当前")
_COMPLETION_PROTOTYPES = (
    "做好了", "做完了", "弄好了", "弄完了", "完成了", "搞定了", "搞好了",
    "准备好了", "收拾好了", "腌好了", "腌完了", "可以了", "差不多了", "ok了", "ok啦",
)
_COMPLETION_STEMS = ("做", "弄", "搞", "完", "成", "定", "妥", "备", "腌", "收", "整", "好", "开", "熟", "变", "干", "散", "匀", "倒", "放", "加", "切", "洗", "搅", "焯")
_TIMER_START_PROTOTYPES = (
    "开始计时", "帮我计时", "给我计时", "开始了", "我腌上了", "我开始腌了", "下锅了",
)
_NEXT_STEP_PROTOTYPES = (
    "下一步", "下步", "下一部", "下一个", "继续下一步", "往下", "接着做", "继续做",
)


def normalize_spoken_text(text: str) -> str:
    """Normalize small ASR formatting differences before intent matching."""
    compact = _SPOKEN_NOISE.sub("", str(text or "").lower())
    for filler in ("我已经", "我这边", "现在", "那个", "就是", "一下", "这个"):
        compact = compact.replace(filler, "")
    return compact


def is_likely_step_completion(text: str) -> bool:
    """Recognize a completion report without relying on one exact wording.

    The score uses both semantic endings (``做完了`` / ``可以了``) and a
    character-level similarity fallback for common ASR near-misses such as
    ``我做哈了``. Questions are deliberately excluded so they cannot advance
    a real cooking step.
    """
    compact = normalize_spoken_text(text)
    if not compact or any(marker in compact for marker in _QUESTION_MARKERS):
        return False
    if any(marker in compact for marker in ("好像", "怎么办", "什么意思", "太咸", "快干", "要干", "粉红", "没熟", "还没")):
        return False
    if any(negative in compact for negative in ("没", "没有", "还没", "不要")):
        return False
    if any(prototype in compact for prototype in _COMPLETION_PROTOTYPES):
        return True
    if compact.endswith(("了", "啦", "喽", "咯", "呢")) and any(stem in compact for stem in _COMPLETION_STEMS):
        return True
    return any(SequenceMatcher(a=compact, b=prototype).ratio() >= 0.67 for prototype in _COMPLETION_PROTOTYPES)


def is_likely_timer_start(text: str) -> bool:
    """Accept natural timing requests and modest ASR wording variation."""
    compact = normalize_spoken_text(text)
    if not compact or any(marker in compact for marker in _QUESTION_MARKERS):
        return False
    if any(marker in compact for marker in ("计时", "记时", "定时", "倒计时", "掐表")) or re.search(r"(?:计|记|定).{0,2}(?:时|钟)", compact):
        return True
    if any(prototype in compact for prototype in _TIMER_START_PROTOTYPES):
        return True
    return any(SequenceMatcher(a=compact, b=prototype).ratio() >= 0.72 for prototype in _TIMER_START_PROTOTYPES)


def is_likely_next_step(text: str) -> bool:
    """Accept common short next-step requests and small ASR substitutions."""
    compact = normalize_spoken_text(text)
    if not compact or any(marker in compact for marker in _QUESTION_MARKERS):
        return False
    if any(prototype in compact for prototype in _NEXT_STEP_PROTOTYPES) or any(
        phrase in compact for phrase in ("前往下一步", "进入下一步", "到下一步")
    ):
        return True
    return any(SequenceMatcher(a=compact, b=prototype).ratio() >= 0.75 for prototype in _NEXT_STEP_PROTOTYPES)


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
    compact = text.replace(" ", "")
    has_timer_marker = any(marker in compact for marker in ("计时", "定时", "记时", "倒计时", "掐表")) or re.search(r"记(?:个|一下)?时", compact) or re.search(r"记.*(?:秒|分)", compact)
    if not has_timer_marker:
        return None
    match = re.search(r"(\d+)\s*(秒|分钟|分)", compact)
    if match:
        value = int(match.group(1))
        return value * 60 if match.group(2) in {"分钟", "分"} else value
    match = re.search(r"([一二两三四五六七八九十])\s*(秒|分钟|分)", compact)
    if match:
        value = _CN_NUMBERS[match.group(1)]
        return value * 60 if match.group(2) in {"分钟", "分"} else value
    return None
