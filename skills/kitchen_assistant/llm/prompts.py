from __future__ import annotations

import json
from typing import Any, Iterable

from kitchen.dish_profiles import profile_prompt_rules


_REQUEST_FIELDS = (
    "requested_dish",
    "available_ingredients",
    "servings",
    "taste_preferences",
    "dietary_restrictions",
    "max_cooking_minutes",
    "available_equipment",
    "difficulty_preference",
    "steak_doneness",
    "steak_thickness_cm",
)

_CANDIDATE_FIELDS = (
    "title",
    "summary",
    "estimated_minutes",
    "difficulty",
    "main_ingredients",
    "main_seasonings",
)

_CANDIDATE_SCHEMA = (
    '{"candidates":[{"title":string,"summary":string,"estimated_minutes":number,'
    '"difficulty":"简单"|"中等","main_ingredients":[string],"main_seasonings":[string],'
    '"missing_ingredients":[string],"match_reason":string}]}'
)

_RECIPE_SCHEMA = (
    '{"title":string,"servings":number,"estimated_minutes":number,'
    '"difficulty":"简单"|"中等","ingredients":[{"name":string,"amount":number|string,'
    '"unit":string,"optional":boolean}],"equipment":[string],"safety_notes":[string],'
    '"steps":[{"step_number":number,"instruction":string,"duration_seconds":number|null,'
    '"heat_level":string|null,"safety_note":string|null}]}'
)


def candidate_messages(request: dict[str, Any]) -> list[dict[str, str]]:
    rules = [
        "任务：生成最多3个适合新手的菜谱候选。只输出合法JSON；不要Markdown、解释、URL、来源或联网声明。",
        "遵守忌口和设备/时间限制，优先使用已有食材。",
        "若requested_dish存在：首个title必须包含该菜名；其余只能是合理变体，不得换成无关菜。缺食材也保留原菜。",
        "available_ingredients缺失或为空=库存未知，此时missing_ingredients必须为[]；仅在库存明确时列出确实缺少的食材。",
        "main_ingredients列全核心食材（如番茄肥牛必须同时有番茄、肥牛），不得把菜名中的核心食材误报为缺少；盐、糖、油、生抽、老抽、醋、料酒、胡椒等列入main_seasonings。",
    ]
    if request.get("available_ingredients") and not request.get("requested_dish"):
        ingredients = "、".join(str(item) for item in request["available_ingredients"])
        rules.append(
            f"用户尚未指定菜名，明确现有食材为：{ingredients}。每个候选都必须实际使用这些食材，"
            "并在main_ingredients或main_seasonings逐项列出；不能忽略其中任何一种，也不能用无关菜替代。"
        )
    rules.extend(profile_prompt_rules(request, stage="candidate"))
    rules.append(f"输出结构：{_CANDIDATE_SCHEMA}")
    return _messages("\n".join(rules), _select(request, _REQUEST_FIELDS))


def recipe_messages(candidate: dict[str, Any], request: dict[str, Any]) -> list[dict[str, str]]:
    rules = [
        "任务：生成面向新手的完整结构化菜谱。只输出合法JSON；不要Markdown、解释、URL、来源、机器人动作或设备控制。",
        "严格遵守servings、忌口、设备和口味；每种食材/调料给明确amount与unit，禁止“适量”“少许”“按口味”。肉类和蔬菜写克数，鸡蛋/土豆等写个数，液体调料写毫升或汤匙/茶匙；可选食材也给用量并标optional=true。",
        "步骤中第一次加入主食材、肉类、蔬菜或调味料时，重复写出对应的明确用量，例如“倒入10毫升生抽、5毫升老抽和1克盐”；不得只写“加调味料”或“加少量盐”。",
        "中式调味只按菜品需要选用盐、油、生抽、老抽、料酒、葱姜蒜等，不要机械堆料。",
        "炒制写清热锅→用油量→下料状态→火力→熟度判断；需要补油时说明。不要声称看见现场或保证已熟。",
        "运行时另行确认肉类解冻，steps不要包含解冻。每步只含一个易记操作阶段；腌制、泡发、切配应分步，同类切配最多合并3项。",
        "duration_seconds用于煮炖焖煎烤蒸炸焯收汁等火候步骤，以及腌制、浸泡、泡发、静置等等待步骤；腌制必须明确时长并填入duration_seconds。洗切拌调味装盘填null。时间须符合家庭烹饪常识，并写可观察的完成状态。",
        "只有后续确实存在可独立提前完成的步骤时，才安排腌制/浸泡期间的辅助准备；辅助准备必须是后续steps中的真实步骤，且不得重复当前腌制步骤已经加入或量取的食材、调料。可选动作仅限洗切配菜、量取后续尚未使用的调料，或在独立汤锅中只烧清水；不要提前热油、空烧锅、炒糖色、油炸或接触正在腌制的肉。若没有符合条件的后续步骤，不要虚构任何并行准备，运行时会提示用户自由安排等待时间。",
        "不要生成“腌制期间准备调料碗”这类空泛步骤；如要准备糖醋汁/调味汁，必须在同一步写明每种调料及用量。一次腌制只对应一个等待计时步骤，不能额外生成第二个“腌制期间”步骤。",
        "step_number从1连续。",
    ]

    if request.get("available_ingredients") and not request.get("requested_dish"):
        ingredients = "、".join(str(item) for item in request["available_ingredients"])
        rules.append(
            f"这是根据用户已有的{ingredients}推荐的菜。完整菜谱必须在ingredients和实际步骤中使用这些食材，"
            "且首次加入时写明用量；不可遗漏。"
        )

    dish_context = {"candidate": candidate, "request": request}
    if _contains(dish_context, ("鸡蛋", "炒蛋", "蛋汤", "蛋面")):
        rules.append("鸡蛋类菜明确鸡蛋个数；盐、油、生抽等给新手可执行用量。")
    if _contains(dish_context, ("肉丝", "猪肉")):
        rules.append("约100克生猪肉丝翻炒初始参考不少于120秒，以完全变色且中心无粉红为准；计时不能替代熟度检查。")
    if _contains(dish_context, ("木耳", "胡萝卜", "蔬菜")):
        rules.append("木耳、胡萝卜等炒至断生/变软通常参考120–240秒，按切配粗细和实际状态调整。")
    if _contains(dish_context, ("米饭", "盖饭", "炒饭")):
        rules.append("普通电饭煲从生米开始不得写十分钟煮熟；快手盖饭/炒饭应明确使用已煮熟米饭。")
    rules.extend(profile_prompt_rules(candidate, request, stage="recipe"))
    rules.append(f"输出结构：{_RECIPE_SCHEMA}")

    payload = {
        "candidate": _select(candidate, _CANDIDATE_FIELDS),
        "request": _select(request, _REQUEST_FIELDS),
    }
    return _messages("\n".join(rules), payload)


def cooking_question_messages(question: str, context: dict[str, Any]) -> list[dict[str, str]]:
    system = (
        "你是谨慎的厨房助手。简洁回答当前烹饪问题，只提供建议，不推进步骤或控制设备。"
        "不要声称联网、看见现场或确认食物已熟。遇到高温、燃气、火灾或伤害风险，先让用户停止操作并寻求现场帮助。"
    )
    return _messages(system, {"question": question, "context": _compact(context)})


def _messages(system: str, payload: dict[str, Any]) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":"))},
    ]


def _select(source: dict[str, Any], fields: Iterable[str]) -> dict[str, Any]:
    return {
        field: compacted
        for field in fields
        if field in source and (compacted := _compact(source[field])) is not None
    }


def _compact(value: Any) -> Any:
    if value is None or value == "" or value == [] or value == {}:
        return None
    if isinstance(value, dict):
        compacted = {key: item for key, raw in value.items() if (item := _compact(raw)) is not None}
        return compacted or None
    if isinstance(value, list):
        compacted = [item for raw in value if (item := _compact(raw)) is not None]
        return compacted or None
    return value


def _contains(payload: Any, keywords: Iterable[str]) -> bool:
    text = json.dumps(payload, ensure_ascii=False)
    return any(keyword in text for keyword in keywords)
