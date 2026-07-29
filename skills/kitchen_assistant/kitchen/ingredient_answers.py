from __future__ import annotations

from typing import Any

from .conversation_intents import is_ingredient_list_request
from .response_builder import feedback
from .session_presenter import recipe_ingredients_text, recipe_metadata


def normalized_candidate_recipe(session: Any, candidate: Any) -> dict[str, Any] | None:
    """读取候选已准备好的详情；失败时不影响候选展示和原会话状态。"""
    try:
        return session.normalizer.normalize(
            session._active_provider.get_recipe_detail(candidate),
            servings=session.request.servings,
        )
    except Exception:
        return None


def candidate_ingredient_lists(session: Any) -> dict[str, str]:
    lists: dict[str, str] = {}
    for candidate in session.recipe_candidates:
        recipe = normalized_candidate_recipe(session, candidate)
        if recipe:
            lists[candidate.candidate_id] = recipe_ingredients_text(recipe)
    return lists


def answer_ingredient_list(
    session: Any,
    text: str,
    *,
    state: str,
) -> dict[str, Any] | None:
    """在任意菜谱会话阶段本地回答食材清单，并保持当前状态。"""
    if not is_ingredient_list_request(text):
        return None

    recipes: list[dict[str, Any]] = []
    if session.current_recipe:
        recipes = [session.current_recipe]
    elif session.selected_candidate is not None:
        recipe = normalized_candidate_recipe(session, session.selected_candidate)
        if recipe:
            recipes = [recipe]
    else:
        for candidate in session.recipe_candidates:
            recipe = normalized_candidate_recipe(session, candidate)
            if recipe:
                recipes.append(recipe)

    if not recipes:
        return None

    if len(recipes) == 1:
        recipe = recipes[0]
        ingredients = recipe_ingredients_text(recipe)
        speech = f"{recipe['name']}一共需要：{ingredients}。"
        display = f"{recipe['name']}完整食材\n{ingredients}"
    else:
        speech_parts = []
        display_lines = ["候选菜谱完整食材"]
        for index, recipe in enumerate(recipes, start=1):
            ingredients = recipe_ingredients_text(recipe)
            speech_parts.append(f"第{index}个，{recipe['name']}需要：{ingredients}")
            display_lines.append(f"{index}. {recipe['name']}：{ingredients}")
        speech = "；".join(speech_parts) + "。"
        display = "\n".join(display_lines)

    extra: dict[str, Any] = {"provider_mode": session._provider_mode()}
    if session.current_recipe:
        extra["current_recipe"] = recipe_metadata(session.current_recipe)
    if state in {"COOKING", "PAUSED"}:
        extra["current_step"] = session.step_index + 1
    return session._result(
        state,
        True,
        feedback(
            speech,
            display,
            robot_action="nod",
            led_effect="warm_white",
            expression="focused",
        ),
        **extra,
    )
