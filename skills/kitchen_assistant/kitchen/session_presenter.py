from __future__ import annotations

from typing import Any

from .models import RecipeCandidate


def ingredient_display(item: dict[str, Any]) -> str:
    amount = str(item.get("amount") or "适量")
    unit = str(item.get("unit") or "")
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
            supply = (
                "现有食材足够"
                if not candidate.missing_ingredients
                else f"缺：{'、'.join(candidate.missing_ingredients)}"
            )
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
