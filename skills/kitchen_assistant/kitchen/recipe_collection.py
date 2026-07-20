from __future__ import annotations

from typing import Any

from .dish_profiles import profile_questions
from .intent_parser import extract_serving_choice
from .recipe_discovery import search_recipes
from .request_parser import apply_updates, parse_updates
from .response_builder import feedback
from .response_phrases import SINGLE_DINER_COMPANIONS
from .states import COLLECTING_INGREDIENTS, COLLECTING_PREFERENCES, COLLECTING_REQUEST


def start_session(session: Any, text: str) -> dict[str, Any]:
    session._reset()
    updates = parse_updates(text)
    apply_updates(session.request, updates)
    welcome = feedback(
        "欢迎使用 AI 厨房助手。", "AI 厨房助手",
        robot_action="wave_hand", led_effect="blue_dynamic", expression="happy",
    )
    if session.request.requested_dish:
        session.state = COLLECTING_PREFERENCES
        return session._with_prefix(welcome, next_preference_response(session))
    if updates.asks_for_recommendation or session.request.available_ingredients:
        session.state = (
            COLLECTING_INGREDIENTS
            if not session.request.available_ingredients
            else COLLECTING_PREFERENCES
        )
        if session.state == COLLECTING_INGREDIENTS:
            return session._with_prefix(
                welcome,
                session._result(COLLECTING_INGREDIENTS, True, session._ask_ingredients()),
            )
        return session._with_prefix(welcome, next_preference_response(session))
    session.state = COLLECTING_REQUEST
    return session._with_prefix(welcome, session._result(
        COLLECTING_REQUEST,
        True,
        feedback(
            "你已经想好要做什么了，还是想根据现有食材推荐呢？",
            "选择方式：指定菜名 / 根据食材推荐",
            robot_action="nod", led_effect="blue", expression="curious", question=True,
        ),
    ))


def collect_request(session: Any, text: str) -> dict[str, Any]:
    updates = parse_updates(text)
    apply_updates(session.request, updates)
    if session.request.requested_dish:
        session.state = COLLECTING_PREFERENCES
        return next_preference_response(session)
    if updates.asks_for_recommendation or session.request.available_ingredients:
        session.state = COLLECTING_INGREDIENTS
        return collect_ingredients(session, text)
    return session._result(
        COLLECTING_REQUEST,
        True,
        feedback(
            "你可以直接说想做的菜名，或者告诉我冰箱里现有的食材，我来帮你想菜谱哦。",
            "请说菜名，或说：我有鸡蛋、番茄和面条",
            robot_action="nod", led_effect="blue", expression="curious", question=True,
        ),
    )


def collect_ingredients(session: Any, text: str) -> dict[str, Any]:
    apply_updates(session.request, parse_updates(text))
    if not session.request.available_ingredients:
        return session._result(COLLECTING_INGREDIENTS, True, session._ask_ingredients())
    session.state = COLLECTING_PREFERENCES
    return next_preference_response(session)


def collect_preferences(session: Any, text: str) -> dict[str, Any]:
    previous_servings = session.request.servings
    updates = parse_updates(text)
    if session.request.servings is None and updates.servings is None:
        updates.servings = extract_serving_choice(text)
    apply_updates(session.request, updates)
    response = next_preference_response(session)
    if previous_servings is None and session.request.servings == 1:
        companion = session.phrases.choose("single_diner", SINGLE_DINER_COMPANIONS)
        if "question" in response:
            response["question"] = f"{companion}{response['question']}"
            response["display"] = f"一个人的小厨房，也有陪伴\n{response['display']}"
            response["robot_action"] = "encourage_gesture"
            response["led_effect"] = "warm_white"
            response["expression"] = "happy"
    return response


def next_preference_response(session: Any) -> dict[str, Any]:
    if session.request.servings is None:
        return session._result(COLLECTING_PREFERENCES, True, feedback(
            "请问是几个人吃？", "请选择人数：1 人 / 2 人 / 3 人",
            robot_action="nod", led_effect="blue", expression="curious", question=True,
        ))
    for question in profile_questions(session.request.requested_dish):
        field = str(question.get("field", ""))
        if field and getattr(session.request, field, None) is None:
            return session._result(COLLECTING_PREFERENCES, True, feedback(
                str(question.get("question", "请补充菜谱所需信息。")),
                str(question.get("display", "请补充菜谱所需信息")),
                robot_action="nod", led_effect="blue", expression="curious", question=True,
            ))
    if not session.request.taste_preferences:
        return session._result(COLLECTING_PREFERENCES, True, feedback(
            "想要正常口味、少盐清淡，还是想吃辣？",
            "请选择口味：正常 / 少盐 / 辣",
            robot_action="nod", led_effect="blue", expression="curious", question=True,
        ))
    return search_recipes(session)
