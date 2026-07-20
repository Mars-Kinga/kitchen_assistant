from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from .ingredient_vocabulary import CONCRETE_SEASONING_TERMS


_CUTTING_OR_WASHING_ACTIONS = ("洗", "切", "去皮", "切块", "切丝", "切片", "切丁")
_HEAT_ACTIONS = (
    "预热", "热油", "加油", "炒", "煎", "炸", "炖", "焖", "蒸", "烤", "烧", "煮",
    "焯", "加热", "收汁", "开火",
)
_FINAL_ACTIONS = ("关火", "盛出", "装盘", "出锅")


def is_waiting_prep_instruction(instruction: str) -> bool:
    """Distinguish “腌制10分钟” from “倒入腌制好的鸡肉丁”."""
    if any(word in instruction for word in ("浸泡", "泡发", "静置", "醒发")):
        return True
    return bool(re.search(r"腌制(?!好)", instruction))


def is_safe_water_boiling_prep(instruction: str) -> bool:
    """Allow only a standalone pot of water to heat during a marinade."""
    has_water = "水" in instruction
    has_pot = any(term in instruction for term in ("锅", "水壶"))
    has_boiling_action = any(term in instruction for term in ("烧开", "煮沸", "沸腾", "加热"))
    unsafe_or_dependent = (
        "油", "炒", "煎", "炸", "炖", "焖", "蒸", "烤", "焯", "收汁",
        "关火", "盛出", "装盘", "放入", "加入", "下入", "后", "再", "然后",
    )
    return has_water and has_pot and has_boiling_action and not any(
        term in instruction for term in unsafe_or_dependent
    )


def is_actionable_parallel_prep(instruction: str) -> bool:
    return any(term in instruction for term in CONCRETE_SEASONING_TERMS + _CUTTING_OR_WASHING_ACTIONS)


def contains_heat_action(instruction: str) -> bool:
    return any(word in instruction for word in _HEAT_ACTIONS)


def is_final_cooking_step(instruction: str) -> bool:
    return any(term in instruction for term in _FINAL_ACTIONS)


def reuses_waiting_step_ingredients(
    candidate: str,
    waiting_step: str,
    ingredient_names: Iterable[str],
) -> bool:
    names = [str(name).strip() for name in ingredient_names if str(name).strip()]
    waiting_names = {name for name in names if name in waiting_step}
    candidate_names = {name for name in names if name in candidate}
    return bool(candidate_names & waiting_names)


def find_parallel_prep_candidate(
    steps: list[dict[str, Any]],
    current_index: int,
    completed_indexes: set[int],
    ingredient_names: Iterable[str],
) -> tuple[int, str] | None:
    if not 0 <= current_index < len(steps):
        return None
    waiting_instruction = str(steps[current_index].get("instruction", ""))
    for index, candidate in enumerate(steps[current_index + 1 :], start=current_index + 1):
        if index in completed_indexes:
            continue
        candidate_text = str(candidate.get("instruction", ""))
        if is_waiting_prep_instruction(candidate_text):
            continue
        if is_safe_water_boiling_prep(candidate_text):
            return index, candidate_text
        if reuses_waiting_step_ingredients(candidate_text, waiting_instruction, ingredient_names):
            continue
        if contains_heat_action(candidate_text) or is_final_cooking_step(candidate_text):
            continue
        if is_actionable_parallel_prep(candidate_text):
            return index, candidate_text
    return None
