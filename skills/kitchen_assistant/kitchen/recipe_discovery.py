from __future__ import annotations

from typing import Any

from .conversation_intents import is_recipe_confirmation
from .request_parser import apply_updates, parse_updates, select_candidate
from .response_builder import feedback
from .session_presenter import candidate_display
from .states import PRESENTING_CANDIDATES, SEARCHING_RECIPES, WAITING_RECIPE_CONFIRMATION


def search_recipes(session: Any) -> dict[str, Any]:
    session.state = SEARCHING_RECIPES
    session._active_provider = session.provider
    session._used_provider_fallback = False
    configured_ai = (
        bool(getattr(session.provider, "supports_ai", False))
        or session._provider_mode() == "ai_generated"
    )
    progress_emitted = False
    if configured_ai:
        progress_emitted = session._emit_progress(feedback(
            "请稍后，正在为你查找菜谱。",
            "请稍后，正在为你查找菜谱",
            robot_action="turn_left", led_effect="blue_dynamic", expression="focused",
        ))
    try:
        session.recipe_candidates = session._active_provider.search_recipes(session.request)
    except Exception:
        if session.request.bypass_cache:
            session.recipe_candidates = []
        else:
            fallback = getattr(session.provider, "fallback", None)
            if fallback is None:
                session.recipe_candidates = []
            else:
                session._active_provider = fallback
                session._used_provider_fallback = True
                session.recipe_candidates = fallback.search_recipes(session.request)
                if configured_ai and session.request.requested_dish:
                    session.recipe_candidates = [
                        candidate
                        for candidate in session.recipe_candidates
                        if session.request.requested_dish in candidate.title
                    ]

    session.state = PRESENTING_CANDIDATES
    actual_mode = session._provider_mode()
    if actual_mode == "local_cache":
        searching = feedback(
            "我找到了几个菜谱。", "已找到菜谱",
            robot_action="nod", led_effect="green_dynamic", expression="happy",
        )
    elif actual_mode == "ai_generated":
        searching = feedback(
            "我正在根据你的需求生成适合的菜谱建议，请稍等哦。",
            "我正在为你生成菜谱建议……",
            robot_action="turn_left", led_effect="blue_dynamic", expression="focused",
        )
    else:
        searching = feedback(
            "我正在查找一些适合你的菜谱，请稍等哦。",
            "正在搜索美味菜谱……",
            robot_action="turn_left", led_effect="blue_dynamic", expression="focused",
        )
    if not session.recipe_candidates:
        if session.request.bypass_cache:
            speech = "联网生成服务暂时不可用，我没有使用已保存结果。请检查联网生成服务配置后再试。"
        else:
            speech = (
                "可能AI生成服务没有生效哦，我没有找到与指定菜名匹配的离线菜谱，但我不会用无关菜谱替代。请配置生成服务，或换个菜名。"
                if session.request.requested_dish
                else "呜呜，好像AI生成服务没有生效，我没有找到离线菜谱，但我不会忽略其中任何一种食材。请配置生成服务，或补充食材后再试。"
            )
        return session._result(
            PRESENTING_CANDIDATES,
            True,
            searching,
            feedback(
                speech, "暂无候选｜没有匹配菜谱",
                robot_action="nod", led_effect="white", expression="neutral",
            ),
            recipe_candidates=[],
            provider_mode=actual_mode,
        )

    count = len(session.recipe_candidates)
    if count == 1:
        generated_speech = (
            "嘿嘿，我生成了一份完整菜谱，直接说“好”或“开始吧”就可以。"
            if actual_mode == "ai_generated"
            else "我找到了一份菜谱，直接说“好”或“开始吧”就可以。"
        )
    else:
        generated_speech = "我列出了几个建议，有想吃的吗？你可以说第一个、第二个或者直接说菜名。"
    candidate_feedback = feedback(
        generated_speech,
        candidate_display(
            session.recipe_candidates,
            provider_mode=actual_mode,
            inventory_known=bool(session.request.available_ingredients),
        ),
        robot_action="nod", led_effect="green_dynamic", expression="happy",
    )
    items = [] if progress_emitted else [searching]
    if session._used_provider_fallback:
        fallback_speech = (
            "生成服务好像暂时不可用了，我会先使用本地菜谱为你推荐哦。"
            if configured_ai
            else "呜呜，目前好像没有配置真实联网服务呢，我先使用本地菜谱为你推荐吧。"
        )
        items.append(feedback(
            fallback_speech, "已切换离线菜谱模式",
            robot_action="nod", led_effect="warm_white", expression="neutral",
        ))
    items.append(candidate_feedback)
    return session._result(
        PRESENTING_CANDIDATES,
        True,
        *items,
        recipe_candidates=[candidate.as_dict() for candidate in session.recipe_candidates],
        provider_mode=actual_mode,
    )


def present_candidates(session: Any, text: str) -> dict[str, Any]:
    updates = parse_updates(text)
    if updates.bypass_cache:
        if not getattr(session.provider, "supports_ai", False):
            return session._result(
                PRESENTING_CANDIDATES,
                True,
                feedback(
                    "现在还没有配置可用的联网生成服务哦，但是我不会假装已经完成上网搜索，也不会改用已保存结果的，嘿嘿。",
                    "联网搜索不可用｜保留当前候选",
                    robot_action="show_concern", led_effect="yellow", expression="alert", question=True,
                ),
                recipe_candidates=[candidate.as_dict() for candidate in session.recipe_candidates],
                provider_mode=session._provider_mode(),
            )
        apply_updates(session.request, updates)
        session.request.excluded_candidate_ids = []
        session.selected_candidate = None
        return search_recipes(session)
    if updates.requested_dish and (
        updates.requested_dish != session.request.requested_dish
        or not session.recipe_candidates
    ):
        apply_updates(session.request, updates)
        session.request.excluded_candidate_ids = []
        session.recipe_candidates = []
        session.selected_candidate = None
        return search_recipes(session)
    if session._has(text, "换一批", "换一个", "不要这个"):
        session.request.excluded_candidate_ids.extend(
            candidate.candidate_id
            for candidate in session.recipe_candidates
            if candidate.candidate_id not in session.request.excluded_candidate_ids
        )
        return search_recipes(session)
    if session._has(text, "更简单", "简单一点", "更快", "快一点"):
        apply_updates(session.request, parse_updates(text))
        session.request.excluded_candidate_ids = []
        return search_recipes(session)
    if session.recipe_candidates and session._has(text, "就这个", "按这个"):
        session.selected_candidate = session.recipe_candidates[0]
        session.state = WAITING_RECIPE_CONFIRMATION
        overview = selected_summary(session)
        if is_recipe_confirmation(text):
            cooking = session._confirm_recipe(text)
            keys = ("speech", "question", "display", "robot_action", "led_effect", "expression")
            overview_feedback = {key: overview[key] for key in keys if key in overview}
            if "steps" in cooking:
                cooking["steps"].insert(0, overview_feedback)
            else:
                cooking["steps"] = [
                    overview_feedback,
                    {key: cooking.pop(key) for key in keys if key in cooking},
                ]
            return cooking
        return overview
    if len(session.recipe_candidates) == 1 and is_recipe_confirmation(text):
        session.selected_candidate = session.recipe_candidates[0]
        session.state = WAITING_RECIPE_CONFIRMATION
        return session._confirm_recipe(text)
    choice = select_candidate(text, [candidate.title for candidate in session.recipe_candidates])
    if choice is not None:
        session.selected_candidate = session.recipe_candidates[choice]
        session.state = WAITING_RECIPE_CONFIRMATION
        return selected_summary(session)
    if len(session.recipe_candidates) == 1:
        return session._result(
            PRESENTING_CANDIDATES,
            True,
            feedback(
                "这是一份可用菜谱，直接说“好”或“开始吧”即可，也可以说“换一个”。",
                "请确认这份菜谱：好 / 开始吧 / 换一个",
                robot_action="nod", led_effect="green_dynamic", expression="curious", question=True,
            ),
            recipe_candidates=[candidate.as_dict() for candidate in session.recipe_candidates],
            provider_mode=session._provider_mode(),
        )
    return session._result(
        PRESENTING_CANDIDATES,
        True,
        feedback(
            "请说第一个、第二个、或者菜名，或者说换一批。",
            "请选择候选：第一个 / 第二个 / 菜名 / 换一批",
            robot_action="nod", led_effect="green_dynamic", expression="curious", question=True,
        ),
        recipe_candidates=[candidate.as_dict() for candidate in session.recipe_candidates],
        provider_mode=session._provider_mode(),
    )


def selected_summary(session: Any) -> dict[str, Any]:
    assert session.selected_candidate is not None
    candidate = session.selected_candidate
    missing = "、".join(candidate.missing_ingredients)
    generated = session._provider_mode() == "ai_generated"
    cached = session._provider_mode() == "local_cache"
    source = (
        "生成方式：根据你的需求生成"
        if generated
        else ("来源：已保存菜谱" if cached else f"来源：{candidate.source_name}")
    )
    inventory = (
        f"缺少：{missing or '无'}"
        if session.request.available_ingredients
        else "食材库存：未提供，确认后给你完整用量清单"
    )
    seasonings = "、".join(candidate.main_seasonings) or "未单列"
    display = (
        f"{candidate.title}\n{source}\n{candidate.estimated_minutes or '未知'} 分钟｜{candidate.difficulty}"
        f"\n主要食材：{'、'.join(candidate.main_ingredients)}"
        f"\n主要调味料：{seasonings}\n{inventory}"
    )
    question = (
        "这是我根据你的需求生成的菜谱，要按照这个开始吗？"
        if generated
        else (
            f"已选{candidate.title}，来源是{candidate.source_name}，预计"
            f"{candidate.estimated_minutes or '未知'}分钟，难度{candidate.difficulty}。要按照这个菜谱开始吗？"
        )
    )
    return session._result(
        WAITING_RECIPE_CONFIRMATION,
        True,
        feedback(
            question, display,
            robot_action="nod", led_effect="green", expression="confident", question=True,
        ),
        selected_candidate=candidate.as_dict(),
        provider_mode=session._provider_mode(),
    )
