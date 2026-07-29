from __future__ import annotations

import re


# 先去掉不会改变意图的标点与空白，再判断否定、疑问和具体意图。
# 这里不尝试穷举完整句子，而是识别日常口语中可重复、可带语气词的核心表达。
_SEPARATORS = re.compile(r"""[，,。！？!?、；;：:"“”'‘’（）()【】\[\]…—~～\s]+""")
_QUESTION_MARKERS = (
    "吗",
    "嘛",
    "怎么",
    "如何",
    "什么",
    "能不能",
    "可不可以",
    "要不要",
    "行不行",
    "好不好",
    "对不对",
    "是不是",
    "确定不",
)
_NEGATIVE_MARKERS = (
    "不好",
    "不太好",
    "不行",
    "不太行",
    "不可以",
    "不能",
    "不想",
    "不要",
    "不用",
    "不对",
    "别",
    "算了",
    "取消",
    "还没",
    "没有",
    "不是",
    "先不",
    "等等",
    "等一下",
    "等会",
    "不开始",
    "不确认",
    "换一个",
    "换一批",
)

# 这些前后缀本身不表示同意，只在后面/前面仍有明确肯定表达时移除。
_LEADING_FILLERS = (
    "麻烦你",
    "麻烦",
    "请你",
    "请",
    "我觉得",
    "我想",
    "那我们就",
    "那我们",
    "咱们就",
    "那就",
    "那么就",
    "那",
    "那么",
    "哎",
    "诶",
)
_TRAILING_FILLERS = (
    "谢谢你",
    "谢谢",
    "辛苦了",
)

# 整句可以由多个肯定语素组成，因此“好的好的”“行行行”
# “可以呀可以呀”“好的开始吧”都能自然识别。
_AFFIRMATIVE_UNIT_PATTERNS = (
    r"好(?:的|滴|哒|嘞|咧|啊|呀|吧|啦|喽|啰|哦|呢|诶)?",
    r"行(?:的|啊|呀|吧|啦|哦|嘞|呢)?",
    r"可以(?:的|啊|呀|吧|啦|哦|呢)?",
    r"阔以",
    r"对(?:的|啊|呀|吧|啦|哦|呢)?",
    r"是(?:的|啊|呀|吧|啦|哦|呢)?",
    r"嗯+",
    r"昂+",
    r"ok(?:ay)?",
    r"没问题",
    r"没毛病",
    r"当然(?:可以|行)?",
    r"必须(?:的)?",
    r"成(?:啊|呀|吧|啦|了)?",
    r"妥(?:了|啦)?",
    r"确定(?:了)?",
    r"确认(?:了)?",
    r"同意",
    r"赞成",
    r"开始(?:吧|啦|了)?",
    r"就开始(?:吧|啦)?",
    r"现在开始(?:吧|啦)?",
    r"开做(?:吧|啦)?",
    r"继续(?:吧|啦)?",
    r"就这个(?:吧|了)?",
    r"就它(?:吧|了)?",
    r"选这个(?:吧|了)?",
    r"按这个(?:做|来)?(?:吧|了)?",
    r"照这个做(?:吧|了)?",
    r"就按这个(?:做|来)?(?:吧|了)?",
    r"就选这个(?:吧|了)?",
    r"就这么做(?:吧|了)?",
    r"就这样(?:吧|了)?",
    r"就这么定(?:吧|了)?",
    r"听你的",
    r"按你说的(?:做|来)",
    r"没意见",
    r"走起",
    r"来吧",
)
_AFFIRMATIVE_SEQUENCE = re.compile(
    rf"^(?:{'|'.join(_AFFIRMATIVE_UNIT_PATTERNS)})+$",
    flags=re.IGNORECASE,
)

_RETRY_PATTERNS = (
    r"重试(?:一下)?(?:吧|呀|啊)?",
    r"再试(?:一次|一下|试)?(?:吧|呀|啊)?",
    r"重新试(?:一次|一下)?(?:吧|呀|啊)?",
    r"重新来(?:一次|一下)?(?:吧|呀|啊)?",
    r"再来(?:一次|一下)(?:吧|呀|啊)?",
    r"重新生成(?:一次|一下)?(?:吧|呀|啊)?",
    r"再生成(?:一次|一下)?(?:吧|呀|啊)?",
)
_RETRY_SEQUENCE = re.compile(
    rf"^(?:{'|'.join(_RETRY_PATTERNS)})+$",
    flags=re.IGNORECASE,
)

_GRATITUDE_PATTERNS = (
    r"谢谢(?:你|啦|了|呀|啊|哦|哈|喽|啰|哟)*",
    r"多谢(?:你|啦|了|呀|啊|哦)*",
    r"感谢(?:你|啦|了|呀|啊|哦)*",
    r"辛苦(?:你)?了",
    r"太感谢(?:你)?了",
    r"谢啦",
    r"谢了",
    r"thanks?",
    r"thankyou",
)
_GRATITUDE_SEQUENCE = re.compile(
    rf"^(?:{'|'.join(_GRATITUDE_PATTERNS)})+$",
    flags=re.IGNORECASE,
)

_STEP_ACKNOWLEDGMENT_PATTERNS = (
    r"知道(?:了|啦)?",
    r"明白(?:了|啦)?",
    r"收到(?:了|啦)?",
    r"懂(?:了|啦)?",
    r"了解(?:了|啦)?",
    r"记住(?:了|啦)?",
)
_STEP_ACKNOWLEDGMENT_SEQUENCE = re.compile(
    rf"^(?:{'|'.join(_STEP_ACKNOWLEDGMENT_PATTERNS)})+$"
)


def compact_command(text: str) -> str:
    return _SEPARATORS.sub("", str(text or "").strip().lower())


def _strip_optional_edges(compact: str) -> str:
    """移除不改变肯定/重试意图的礼貌前后缀。"""
    value = compact
    changed = True
    while changed and value:
        changed = False
        for prefix in _LEADING_FILLERS:
            if value.startswith(prefix) and len(value) > len(prefix):
                value = value[len(prefix):]
                changed = True
                break
    changed = True
    while changed and value:
        changed = False
        for suffix in _TRAILING_FILLERS:
            if value.endswith(suffix) and len(value) > len(suffix):
                value = value[:-len(suffix)]
                changed = True
                break
    return value


def _is_negative_or_question(compact: str) -> bool:
    return (
        not compact
        or any(marker in compact for marker in _NEGATIVE_MARKERS)
        or any(marker in compact for marker in _QUESTION_MARKERS)
    )


def is_affirmative(text: str) -> bool:
    """识别自然肯定答复；重复、语气词和礼貌前后缀不影响结果。"""
    compact = compact_command(text)
    if _is_negative_or_question(compact):
        return False
    normalized = _strip_optional_edges(compact)
    return bool(_AFFIRMATIVE_SEQUENCE.fullmatch(normalized))


def is_retry(text: str) -> bool:
    compact = compact_command(text)
    if _is_negative_or_question(compact):
        return False
    return bool(_RETRY_SEQUENCE.fullmatch(_strip_optional_edges(compact)))


def is_recipe_confirmation(text: str) -> bool:
    return is_affirmative(text) or is_retry(text)


def is_negative(text: str) -> bool:
    compact = compact_command(text)
    return bool(compact) and any(marker in compact for marker in _NEGATIVE_MARKERS)


def is_gratitude(text: str) -> bool:
    compact = compact_command(text)
    return bool(compact) and bool(_GRATITUDE_SEQUENCE.fullmatch(compact))


def is_step_acknowledgment(text: str) -> bool:
    compact = compact_command(text)
    if _is_negative_or_question(compact):
        return False
    normalized = _strip_optional_edges(compact)
    return (
        bool(_STEP_ACKNOWLEDGMENT_SEQUENCE.fullmatch(normalized))
        or is_affirmative(normalized)
    )


def is_ingredient_list_request(text: str) -> bool:
    """识别用户索要当前菜谱完整食材/材料/配料清单的表达。"""
    compact = compact_command(text)
    if not compact:
        return False
    nouns = ("食材", "材料", "配料", "调料", "原料")
    if not any(noun in compact for noun in nouns):
        return False
    request_markers = (
        "什么",
        "哪些",
        "有哪",
        "需要",
        "要用",
        "清单",
        "列表",
        "再说一遍",
        "再念一遍",
    )
    return any(marker in compact for marker in request_markers)
