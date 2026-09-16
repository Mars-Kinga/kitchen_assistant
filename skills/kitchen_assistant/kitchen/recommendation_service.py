from __future__ import annotations

from typing import Any

from .dietary_rules import ingredient_conflicts
from .ingredient_vocabulary import ingredient_present, inventory_ingredient_present, split_main_foods_and_seasonings
from .models import RecipeCandidate, RecipeSearchRequest
from .pantry_defaults import is_common_seasoning, missing_pantry_ingredients, uses_unavailable_ingredients


def rank_recipes(recipes: list[dict[str, Any]], request: RecipeSearchRequest) -> list[RecipeCandidate]:
    ranked: list[tuple[int, RecipeCandidate]] = []
    for raw in recipes:
        ingredients = [str(item.get("name", "")) for item in raw.get("ingredients", [])]
        required_ingredients = [str(item.get("name", "")) for item in raw.get("ingredients", [])
                                if not item.get("optional")]
        instructions = [str(step.get("instruction", "")) for step in raw.get("steps", [])]
        used_inventory = [item for item in request.available_ingredients
                          if inventory_ingredient_present(item, required_ingredients)
                          and inventory_ingredient_present(item, instructions)]
        if ingredient_conflicts(ingredients, request.dietary_restrictions):
            continue
        if uses_unavailable_ingredients(ingredients, request.unavailable_ingredients):
            continue
        if _dietary_requirement_is_unverified(raw, request):
            continue
        if _equipment_is_unavailable(raw, request):
            continue
        if not request.requested_dish and request.available_ingredients:
            if not used_inventory or missing_pantry_ingredients(required_ingredients, request.available_ingredients):
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
        pantry_mode = bool(request.available_ingredients and not request.requested_dish)
        missing = [] if not request.available_ingredients else [
            name for name in ingredients
            if not any(inventory_ingredient_present(item, [name]) for item in request.available_ingredients)
            and name not in {"盐", "食用油"}
            and not (pantry_mode and (is_common_seasoning(name) or name in {"水", "清水", "热水", "开水"}))
        ]
        score += len(present) * 20 - len(missing) * 8
        if not request.requested_dish and request.available_ingredients:
            # Inventory coverage is the primary ordering key, before taste/time.
            score += len(used_inventory) * 10000
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
        if pantry_mode:
            reason = ("能够使用你现有的全部确认食材。" if len(used_inventory) == len(request.available_ingredients)
                      else f"可使用现有的{'、'.join(used_inventory)}，其他确认食材没用到。")
        main_ingredients, main_seasonings = split_main_foods_and_seasonings(ingredients)
        candidate = RecipeCandidate(
            candidate_id=recipe_id,
            title=str(raw["name"]),
            source_name=str(raw.get("source_name") or "本地示例菜谱"),
            source_url=raw.get("source_url"),
            summary=str(raw.get("summary") or "本地菜谱。"),
            estimated_minutes=minutes if isinstance(minutes, int) else None,
            difficulty=str(raw.get("difficulty") or "简单"),
            main_ingredients=main_ingredients,
            missing_ingredients=missing,
            match_reason=reason,
            main_seasonings=main_seasonings,
            unused_ingredients=[
                item for item in request.available_ingredients
                if (item not in used_inventory if not request.requested_dish
                    else not ingredient_present(item, ingredients))
            ],
        )
        ranked.append((score, candidate))
    results = [candidate for _, candidate in sorted(ranked, key=lambda pair: (-pair[0], pair[1].title))[:3]]
    return results


def _reason(present: list[str], missing: list[str], request: RecipeSearchRequest, raw: dict[str, Any]) -> str:
    if request.requested_dish == raw.get("name"):
        return "与你指定的菜名一致。"
    if present and not missing:
        return "能够使用你现有的全部主要食材。"
    if present:
        return f"可利用现有的{'、'.join(present)}，只缺少少量主要食材。"
    return "符合当前的本地菜谱筛选条件。"

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
