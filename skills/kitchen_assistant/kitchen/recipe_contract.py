from __future__ import annotations

from collections.abc import Iterable
from typing import Any


REQUEST_FIELDS = (
    "requested_dish",
    "available_ingredients",
    "servings",
    "taste_preferences",
    "dietary_restrictions",
    "max_cooking_minutes",
    "available_equipment",
    "unavailable_equipment",
    "equipment_only",
    "difficulty_preference",
    "steak_doneness",
    "steak_thickness_cm",
    "excluded_candidate_ids",
)

ALLOWED_DIFFICULTIES = {"简单", "中等"}
VAGUE_QUANTITY_MARKERS = ("适量", "少量", "少许", "按口味")
MAX_TEXT_LENGTH = 120
MAX_LIST_ITEMS = 12
MIN_ESTIMATED_MINUTES = 1
MAX_ESTIMATED_MINUTES = 240

RECIPE_SCHEMA_TEXT = (
    '{"title":string,"servings":integer,"estimated_minutes":integer,'
    '"difficulty":"简单"|"中等","ingredients":[{"name":string,"amount":number|string,'
    '"unit":string,"optional":boolean}],"equipment":[string],"safety_notes":[string],'
    '"steps":[{"step_number":integer,"instruction":string,"duration_seconds":number|null,'
    '"heat_level":string|null,"safety_note":string|null}]}'
)

RECIPE_BUNDLE_SCHEMA_TEXT = (
    '{"candidates":[{"title":string,"summary":string,"estimated_minutes":integer,'
    '"difficulty":"简单"|"中等","main_ingredients":[string],"main_seasonings":[string],'
    '"missing_ingredients":[string],"match_reason":string,"recipe":'
    f'{RECIPE_SCHEMA_TEXT}'
    '}]}'
)


def candidate_limit(request: dict[str, Any]) -> int:
    """A named dish needs one answer; pantry discovery may offer choices."""
    return 1 if request.get("requested_dish") else 3


def bundle_prompt_rules(request: dict[str, Any]) -> list[str]:
    limit = candidate_limit(request)
    count_rule = (
        "用户指定了菜名，只生成1个完整候选，不生成同菜变体。"
        if limit == 1
        else "用户未指定菜名，生成1至3个不同候选。"
    )
    rules = [
        "一次生成候选及完整recipe。只输出合法JSON；不要Markdown、解释、URL、来源或设备控制。",
        count_rule,
        "requested_dish存在时title必须含完整菜名，缺料也不能换菜；不得重复excluded_candidate_ids中的菜名。文本字段各不超过120字。",
        "每个candidate都必须有recipe，二者title完全一致；详情不完整就不输出该候选。",
        "servings须完全一致；estimated_minutes为1至240的整数且不超过max_cooking_minutes；difficulty仅简单或中等。",
        "食材调料不得命中dietary_restrictions；equipment不得命中unavailable_equipment；equipment_only=true时只用available_equipment。",
        "available_ingredients为空=库存未知且missing_ingredients=[]；库存明确且未指定菜名时必须用完全部库存食材。",
        "main_ingredients列核心食材，盐糖油酱醋料酒胡椒列main_seasonings；两者都须出现在recipe.ingredients。",
        "每项ingredient须有name、明确amount、非空unit和boolean optional，禁用适量/少量/少许/按口味；首次加入时instruction重复用量。",
        "recipe以6至10步为目标；step_number从1连续；每步一个阶段。空泛的准备调料步骤禁止，调味汁须列出全部用量。",
        "duration_seconds仅用于加热或等待，洗切拌调味装盘填null；时间合理并写可观察状态。steps不含解冻，不保证已熟。",
        "输出前自检人数、时间、忌口、厨具、库存、用量、标题和JSON；不合格候选不要输出。",
        f"输出结构：{RECIPE_BUNDLE_SCHEMA_TEXT}",
    ]
    return rules


def validate_text(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if not text or len(text) > MAX_TEXT_LENGTH:
        raise ValueError(f"{label}无效")
    return text


def validate_estimated_minutes(value: Any, *, maximum: int | None = None) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError("预计时间必须是整数")
    if not MIN_ESTIMATED_MINUTES <= value <= MAX_ESTIMATED_MINUTES:
        raise ValueError("预计时间无效")
    if maximum is not None and value > maximum:
        raise ValueError("预计时间超过用户限制")
    return value


def validate_raw_recipe(raw: Any, request: Any) -> None:
    if not isinstance(raw, dict):
        raise ValueError("菜谱必须是对象")
    validate_text(raw.get("title"), "菜名")
    servings = raw.get("servings")
    if not isinstance(servings, int) or isinstance(servings, bool) or servings <= 0:
        raise ValueError("菜谱人数无效")
    requested_servings = getattr(request, "servings", None)
    if requested_servings is not None and servings != requested_servings:
        raise ValueError("完整菜谱人数与请求不一致")
    validate_estimated_minutes(
        raw.get("estimated_minutes"),
        maximum=getattr(request, "max_cooking_minutes", None),
    )
    if raw.get("difficulty") not in ALLOWED_DIFFICULTIES:
        raise ValueError("菜谱难度无效")

    ingredients = raw.get("ingredients")
    if not isinstance(ingredients, list) or not ingredients:
        raise ValueError("菜谱必须包含食材")
    for item in ingredients:
        if not isinstance(item, dict):
            raise ValueError("食材无效")
        validate_text(item.get("name"), "食材")
        amount = validate_text(item.get("amount"), "食材用量")
        if any(marker in amount for marker in VAGUE_QUANTITY_MARKERS):
            raise ValueError("食材必须给出明确用量")
        validate_text(item.get("unit"), "食材单位")
        if not isinstance(item.get("optional"), bool):
            raise ValueError("食材optional必须是布尔值")

    steps = raw.get("steps")
    if not isinstance(steps, list) or not steps:
        raise ValueError("菜谱必须包含步骤")
    for index, step in enumerate(steps, start=1):
        if not isinstance(step, dict):
            raise ValueError("步骤说明无效")
        instruction = validate_text(step.get("instruction"), "步骤")
        if any(marker in instruction for marker in VAGUE_QUANTITY_MARKERS):
            raise ValueError("步骤用量必须明确")
        if any(marker in instruction for marker in ("准备调料碗", "准备调料", "准备酱汁")):
            from .ingredient_vocabulary import CONCRETE_SEASONING_TERMS

            if not any(marker in instruction for marker in CONCRETE_SEASONING_TERMS):
                raise ValueError("调料准备步骤必须写明具体调料和用量")
        step["step_number"] = index
        if step.get("duration_seconds") is not None and not isinstance(step["duration_seconds"], (int, float)):
            step["duration_seconds"] = None
        if step.get("heat_level") is not None and not isinstance(step["heat_level"], str):
            step["heat_level"] = None
        if step.get("safety_note") is not None and not isinstance(step["safety_note"], str):
            step["safety_note"] = None


def string_list(value: Any, *, limit: int = MAX_LIST_ITEMS) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if isinstance(item, str) and item.strip()][:limit]


def contains_vague_quantity(value: str) -> bool:
    return any(marker in value for marker in VAGUE_QUANTITY_MARKERS)


def all_present(required: Iterable[str], actual: Iterable[str]) -> bool:
    from .ingredient_vocabulary import ingredient_present

    labels = list(actual)
    return all(ingredient_present(item, labels) for item in required)
