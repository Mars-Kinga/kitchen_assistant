from __future__ import annotations

import re
from typing import Any

from .models import RecipeCandidate


def ingredient_display(item: dict[str, Any]) -> str:
    amount = str(item.get("amount") if item.get("amount") is not None else "适量").strip() or "适量"
    unit = str(item.get("unit") or "").strip()
    qualitative = re.match(r"^(适量|少量|少许|若干|按口味)", amount)
    if qualitative:
        if re.fullmatch(r"(适量|少量|少许|若干)[块个片瓣勺碗份把圈]*", amount):
            amount = qualitative.group(1)
        unit = ""
    elif unit:
        while amount.endswith(unit + unit):
            amount = amount[:-len(unit)]
        if amount.endswith(unit) or re.search(r"(?:\d|[一二两三四五六七八九十半])\s*(?:毫升|毫克|千克|公斤|汤匙|茶匙|汤勺|小勺|勺|匙|碗|杯|克|斤|两|个|块|片|瓣|份|把|圈|kg|g|ml)$", amount, re.I):
            unit = ""
    optional = "（可选）" if item.get("optional") else ""
    return f"{item.get('name', '食材')} {amount}{unit}{optional}"


def recipe_ingredients_text(recipe: dict[str, Any]) -> str:
    return "、".join(ingredient_display(item) for item in recipe.get("ingredients", []))


def candidate_display(
    candidates: list[RecipeCandidate],
    *,
    provider_mode: str,
    inventory_known: bool,
    ingredient_lists: dict[str, str] | None = None,
) -> str:
    heading = (
        "我为你生成的菜谱"
        if provider_mode == "ai_generated"
        else ("推荐菜谱" if provider_mode == "local_cache" else "推荐菜谱（本地）")
    )
    lines = [heading]
    single_candidate = len(candidates) == 1
    for index, candidate in enumerate(candidates, start=1):
        ingredients = (ingredient_lists or {}).get(candidate.candidate_id)
        if ingredients:
            supply = "完整食材如下"
        elif not inventory_known:
            supply = "食材见详情"
        else:
            supply = f"没用到：{'、'.join(candidate.unused_ingredients) or '无'}"
        prefix = "" if single_candidate else f"{index}. "
        lines.append(
            f"{prefix}{candidate.title}｜{candidate.estimated_minutes or '?'} 分钟｜"
            f"{candidate.difficulty}｜{supply}"
        )
        if ingredients:
            lines.append(f"食材：{ingredients}")
    return "\n".join(lines)


def recipe_metadata(recipe: dict[str, Any] | None) -> dict[str, Any] | None:
    if not recipe:
        return None
    return {
        key: recipe.get(key)
        for key in (
            "recipe_id", "name", "source_name", "source_url", "estimated_time_minutes", "difficulty",
        )
    }


def public_metadata(value: Any) -> Any:
    """Hide internal provider branding from user-facing runtime results."""
    if isinstance(value, list):
        return [public_metadata(item) for item in value]
    if isinstance(value, dict):
        cleaned = {key: public_metadata(item) for key, item in value.items()}
        if cleaned.get("source_name") == "千问 AI 生成":
            cleaned.pop("source_name", None)
        return cleaned
    return value
