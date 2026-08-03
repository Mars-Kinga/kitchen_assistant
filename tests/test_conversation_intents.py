from __future__ import annotations

import sys
from pathlib import Path

import pytest


SKILL_ROOT = Path(__file__).resolve().parents[1] / "skills" / "kitchen_assistant"
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

from kitchen.conversation_intents import (  # noqa: E402
    is_affirmative,
    is_gratitude,
    is_negative,
    is_recipe_confirmation,
    is_retry,
    is_step_acknowledgment,
)


@pytest.mark.parametrize(
    "utterance",
    [
        "好",
        "好的好的",
        "好呀好呀",
        "行行行",
        "可以可以",
        "可以呀可以呀",
        "对对对",
        "嗯嗯嗯",
        "OK OK",
        "那就开始吧",
        "那我们就按这个做吧",
        "麻烦你开始吧",
        "就这么定了",
        "听你的",
        "按你说的来",
        "没意见",
        "好的，谢谢",
    ],
)
def test_natural_affirmative_variants(utterance: str) -> None:
    assert is_affirmative(utterance)
    assert is_recipe_confirmation(utterance)


@pytest.mark.parametrize(
    "utterance",
    [
        "",
        "好难",
        "好像不行",
        "不太好",
        "不可以",
        "先不要开始",
        "可以吗",
        "好不好",
        "要不要开始",
        "我再想想",
        "等等",
        "换一个",
        "好的菜谱",
    ],
)
def test_affirmative_does_not_accept_questions_negatives_or_unrelated_text(
    utterance: str,
) -> None:
    assert not is_affirmative(utterance)


@pytest.mark.parametrize(
    "utterance",
    [
        "重试",
        "重试一下吧",
        "再试试",
        "麻烦你再试一次",
        "重新来一下",
        "再生成一次",
    ],
)
def test_natural_retry_variants(utterance: str) -> None:
    assert is_retry(utterance)
    assert is_recipe_confirmation(utterance)


@pytest.mark.parametrize(
    "utterance",
    ["谢谢", "谢谢你呀", "多谢啦", "太感谢你了", "辛苦你了", "thanks"],
)
def test_natural_gratitude_variants(utterance: str) -> None:
    assert is_gratitude(utterance)


@pytest.mark.parametrize(
    "utterance",
    ["好的好的", "知道了知道了", "明白啦", "收到收到", "懂了", "记住了"],
)
def test_natural_step_acknowledgment_variants(utterance: str) -> None:
    assert is_step_acknowledgment(utterance)


@pytest.mark.parametrize(
    "utterance",
    ["不行", "不太好", "先不要", "还没完成", "换一个", "算了"],
)
def test_natural_negative_variants(utterance: str) -> None:
    assert is_negative(utterance)
