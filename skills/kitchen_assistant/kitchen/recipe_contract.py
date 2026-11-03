from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .pantry_defaults import pantry_meal_rule, pantry_seasoning_rule


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
    "unavailable_ingredients",
)

ALLOWED_DIFFICULTIES = {"简单", "中等"}
# “少许”是厨房中可执行的微量表达，例如“少许油润锅”或“撒少许葱花”。
# 仍拒绝无法判断总量的“适量 / 少量 / 按口味”。
VAGUE_QUANTITY_MARKERS = ("适量", "少量", "按口味")
VAGUE_INGREDIENT_AMOUNT_MARKERS = (*VAGUE_QUANTITY_MARKERS, "少许")
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
    """Generate choices for pantry discovery, but one answer for a named dish."""
    if request.get("available_ingredients") and not request.get("requested_dish"):
        return 3
    return 1


def bundle_prompt_rules(request: dict[str, Any]) -> list[str]:
    limit = candidate_limit(request)
    count_rule = (
        "生成1至3个不同的完整候选，优先生成2个以保证完整输出；有1个可行方案也返回。优先覆盖全部available_ingredients，允许部分未使用，按实际使用数量从多到少排列。"
        if limit == 3
        else "只生成1个完整候选，不生成同菜变体或额外候选。"
    )
    rules = [
        "一次生成候选及完整recipe。只输出合法JSON；不要Markdown、解释、URL、来源或设备控制。",
        count_rule,
        "requested_dish存在时title必须含完整菜名，缺料也不能换菜；不得重复excluded_candidate_ids中的菜名。文本字段各不超过120字。",
        "每个candidate都必须有recipe，二者title完全一致；详情不完整就不输出该候选。",
        "servings须完全一致；estimated_minutes为1至240的整数且不超过max_cooking_minutes；difficulty仅简单或中等。",
        "食材调料不得命中dietary_restrictions或使用unavailable_ingredients；equipment不得命中unavailable_equipment；equipment_only=true时只用available_equipment。",
        "available_ingredients为空=库存未知且missing_ingredients=[]；库存明确且未指定菜名时优先使用全部确认食材，也允许部分食材没用到。每份方案至少实际使用一种确认食材，不能只写在简介或配料表；实际使用食材须出现在recipe.ingredients及步骤中。可分区、分段或配菜搭配，不必强行混炒。",
        "main_ingredients列核心食材，盐糖油酱醋料酒胡椒列main_seasonings；两者都须出现在recipe.ingredients。",
        "每项ingredient须有name、明确amount、非空unit和boolean optional，amount不得出现适量/少量/少许/按口味。instruction允许用“少许”描述微量润锅油或点缀香料，仍禁止适量/少量/按口味；分批操作只写“分批下锅”。首次加入主要食材和调料时优先照抄ingredients中的准确用量。",
        "recipe保持完整的6至10步；step_number从1连续。面向零基础新手逐项写清动作顺序、准确用量、火力、时长和可观察完成状态，不把切配、调味、下锅等多个阶段压缩成一句；每步只做一个阶段且instruction不超过120字。summary和match_reason各不超过30字，safety_note不超过40字。空泛的准备调料步骤禁止，调味汁须列出全部用量。",
        "duration_seconds仅用于加热或等待，洗切拌调味装盘填null；时间合理并写可观察状态。steps不含解冻，不保证已熟。",
        "输出前自检人数、时间、忌口、厨具、库存、用量、标题和JSON；不合格候选不要输出。",
        f"输出结构：{RECIPE_BUNDLE_SCHEMA_TEXT}",
    ]
    if limit == 3:
        rules.insert(-2, pantry_meal_rule(request))
        rules.insert(-2, "多菜组合不受单道菜6至10步限制：两道可写12至18步，三道可写18至30步；每道菜保留完整新手细节。ingredients和实际使用步骤保留用户食材名称，避免只用‘蔬菜’‘肉’等泛称；每道都写明用量和做法。")
    seasoning_rule = pantry_seasoning_rule(request)
    if seasoning_rule:
        rules.insert(-2, seasoning_rule)
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
        if any(marker in amount for marker in VAGUE_INGREDIENT_AMOUNT_MARKERS):
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
        try:
            instruction = validate_text(step.get("instruction"), "步骤")
        except ValueError as exc:
            raise ValueError(f"步骤无效（第{index}步：说明为空或超过{MAX_TEXT_LENGTH}字，请拆成独立操作）") from exc
        vague_marker = next(
            (marker for marker in VAGUE_QUANTITY_MARKERS if marker in instruction),
            None,
        )
        if vague_marker:
            preview = instruction[:50]
            raise ValueError(
                f"步骤用量必须明确（第{index}步含“{vague_marker}”：{preview}）"
            )
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
