from __future__ import annotations

from typing import Any

from .dietary_rules import ingredient_conflicts
from .ingredient_vocabulary import ingredient_present, split_main_foods_and_seasonings
from .models import RecipeCandidate, RecipeSearchRequest


def rank_recipes(recipes: list[dict[str, Any]], request: RecipeSearchRequest) -> list[RecipeCandidate]:
    ranked: list[tuple[int, RecipeCandidate]] = []
    for raw in recipes:
        ingredients = [str(item.get("name", "")) for item in raw.get("ingredients", [])]
        if ingredient_conflicts(ingredients, request.dietary_restrictions):
            continue
        if _dietary_requirement_is_unverified(raw, request):
            continue
        if _equipment_is_unavailable(raw, request):
            continue
        # If the user has not chosen a dish and explicitly supplied a pantry,
        # recommending a dish that ignores part of that pantry is misleading.
        # Return no offline candidate instead, so the configured AI provider
        # can compose an appropriate dish from the stated ingredients.
        if not request.requested_dish and request.available_ingredients:
            if any(not ingredient_present(item, ingredients) for item in request.available_ingredients):
                continue
        recipe_id = str(raw["recipe_id"])
        if recipe_id in request.excluded_candidate_ids:
            continue
        score = 0
        if request.requested_dish:
            if request.requested_dish == raw.get("name"):
                score += 1000
            elif request.requested_dish in str(raw.get("name")):
                score += 500
            else:
                score -= 100
        present = [name for name in ingredients if name in request.available_ingredients]
        # An omitted inventory is unknown, not an empty kitchen. Do not turn a
        # requested dish into a misleading shopping list in that situation.
        missing = [] if not request.available_ingredients else [name for name in ingredients if name not in request.available_ingredients and name not in {"盐", "食用油"}]
        score += len(present) * 20 - len(missing) * 8
        minutes = raw.get("estimated_time_minutes")
        if request.max_cooking_minutes and isinstance(minutes, int):
            score += 10 if minutes <= request.max_cooking_minutes else -30
        if request.difficulty_preference == "简单" and raw.get("difficulty") == "简单":
            score += 12
        if "辣" in request.taste_preferences and "辣椒" in ingredients:
            score += 6
        if "少盐" in request.taste_preferences and "辣椒" not in ingredients:
            score += 2
        equipment = set(raw.get("equipment", []))
        if request.available_equipment:
            score += 8 if equipment & set(request.available_equipment) else -20
        reason = _reason(present, missing, request, raw)
        main_ingredients, main_seasonings = split_main_foods_and_seasonings(ingredients)
        candidate = RecipeCandidate(
            candidate_id=recipe_id,
            title=str(raw["name"]),
            source_name=str(raw.get("source_name") or "本地示例菜谱"),
            source_url=raw.get("source_url"),
            summary=str(raw.get("summary") or "离线示例菜谱。"),
            estimated_minutes=minutes if isinstance(minutes, int) else None,
            difficulty=str(raw.get("difficulty") or "简单"),
            main_ingredients=main_ingredients,
            missing_ingredients=missing,
            match_reason=reason,
            main_seasonings=main_seasonings,
        )
        ranked.append((score, candidate))
    return [candidate for _, candidate in sorted(ranked, key=lambda pair: (-pair[0], pair[1].title))[:3]]


def _reason(present: list[str], missing: list[str], request: RecipeSearchRequest, raw: dict[str, Any]) -> str:
    if request.requested_dish == raw.get("name"):
        return "与你指定的菜名一致。"
    if present and not missing:
        return "能够使用你现有的全部主要食材。"
    if present:
        return f"可利用现有的{'、'.join(present)}，只缺少少量主要食材。"
    return "符合当前的离线示例筛选条件。"

def _equipment_is_unavailable(raw: dict[str, Any], request: RecipeSearchRequest) -> bool:
    equipment = [str(item) for item in raw.get("equipment", [])]
    if any(item in set(request.unavailable_equipment) for item in equipment):
        return True
    if not request.equipment_only or not request.available_equipment or not equipment:
        return False
    return not any(item in set(request.available_equipment) for item in equipment)


def _dietary_requirement_is_unverified(raw: dict[str, Any], request: RecipeSearchRequest) -> bool:
    """Never label a local recipe low-fat/etc. without explicit data tags."""
    requirements = {item for item in request.dietary_restrictions if item in {"低脂", "高蛋白", "控糖", "素食"}}
    if not requirements:
        return False
    tags = {str(item) for item in raw.get("dietary_tags", []) if str(item)}
    return not requirements.issubset(tags)
