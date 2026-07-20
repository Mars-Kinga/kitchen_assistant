from __future__ import annotations

from typing import Any

from .conversation_intents import is_recipe_confirmation
from .cooking_preconditions import animal_protein_names, recipe_respects_restrictions
from .recipe_discovery import present_candidates, search_recipes
from .request_parser import apply_updates, parse_updates
from .response_builder import feedback
from .session_presenter import ingredient_display, recipe_metadata
from .states import COOKING, PRESENTING_CANDIDATES, WAITING_MEAT_THAW, WAITING_RECIPE_CONFIRMATION


def confirm_recipe(session: Any, text: str) -> dict[str, Any]:
    """确认已选菜谱；生成详情必须已在候选搜索阶段准备完成。"""
    if session._has(text, "更简单", "简单一点", "更快", "快一点"):
        apply_updates(session.request, parse_updates(text))
        session.request.excluded_candidate_ids = []
        return search_recipes(session)
    if session._has(text, "换", "不要"):
        session.state = PRESENTING_CANDIDATES
        return present_candidates(session, "换一批" if "换一批" in text else "")
    if not is_recipe_confirmation(text):
        return session._result(
            WAITING_RECIPE_CONFIRMATION,
            True,
            feedback(
                "你可以直接说“好”“开始”或“就这个”；也可以说“换一个”。",
                "等待确认：好 / 开始 / 换一个",
                robot_action="nod",
                led_effect="green",
                expression="confident",
                question=True,
            ),
            selected_candidate=session.selected_candidate.as_dict() if session.selected_candidate else None,
            provider_mode=session._provider_mode(),
        )

    assert session.selected_candidate is not None
    try:
        session.current_recipe = session.normalizer.normalize(
            session._active_provider.get_recipe_detail(session.selected_candidate),
            servings=session.request.servings,
        )
    except Exception as exc:
        # 不用无关本地菜替换用户已选菜名。保留候选，让“重试”仍针对同一详情。
        print(f"[厨房助手-详情失败] {session._safe_detail_error(exc)}")
        session.current_recipe = None
        session.state = WAITING_RECIPE_CONFIRMATION
        return session._result(
            WAITING_RECIPE_CONFIRMATION,
            True,
            feedback(
                f"{session.selected_candidate.title}的完整菜谱读取或校验失败，我没有换成其他菜。"
                "你可以说“重试”，或者说“换一个”。",
                f"{session.selected_candidate.title}详情失败｜重试 / 换一个",
                robot_action="show_concern",
                led_effect="yellow",
                expression="alert",
                question=True,
            ),
            selected_candidate=session.selected_candidate.as_dict(),
        )

    if not recipe_respects_restrictions(
        session.current_recipe, session.request.dietary_restrictions
    ):
        session.current_recipe = None
        session.state = PRESENTING_CANDIDATES
        return session._result(
            PRESENTING_CANDIDATES,
            True,
            feedback(
                "菜谱详情与刚才记录的忌口冲突，我不会让你按这个菜谱继续。请换一个候选。",
                "菜谱与忌口冲突｜请换一个",
                robot_action="show_concern",
                led_effect="yellow",
                expression="alert",
            ),
            recipe_candidates=[candidate.as_dict() for candidate in session.recipe_candidates],
        )

    meats = animal_protein_names(session.current_recipe)
    if meats:
        session.state = WAITING_MEAT_THAW
        meat_text = "、".join(meats)
        cache_name = session._new_cache_filename()
        cache_prefix = "好的，我准备好菜谱了。" if cache_name else ""
        cache_line = "\n已准备好菜谱。" if cache_name else ""
        if cache_name and session._cache_candidate_count() > 1:
            cache_line = f"\n已准备好菜谱（同一文件含 {session._cache_candidate_count()} 个候选）。"
        return session._result(
            WAITING_MEAT_THAW,
            True,
            feedback(
                f"{cache_prefix}这道菜要用到{meat_text}。如果它来自冷冻，请确认已经完全解冻；"
                "如果是新鲜食材，可以直接告诉我。",
                f"确认动物性食材状态：{meat_text}\n已解冻 / 新鲜食材 / 还没解冻{cache_line}",
                robot_action="show_concern",
                led_effect="yellow",
                expression="alert",
                question=True,
            ),
            current_recipe=recipe_metadata(session.current_recipe),
            recipe_cache_file=cache_name,
        )
    return begin_cooking(session)


def confirm_meat_precondition(session: Any, text: str) -> dict[str, Any]:
    compact = text.replace(" ", "")
    if any(word in compact for word in ("还没", "没有", "未解冻", "冻着")):
        return session._result(
            WAITING_MEAT_THAW,
            True,
            feedback(
                "最快的安全办法是用微波炉解冻档分段解冻，每次短时间检查；解冻后立刻烹饪。"
                "没有微波炉时，把密封的肉放进冷水中，每 30 分钟换水。不要放在室温台面或用热水解冻。"
                "解冻好后告诉我。",
                "肉未解冻｜微波解冻档或密封冷水解冻｜不要室温、热水解冻",
                robot_action="show_concern",
                led_effect="yellow",
                expression="alert",
            ),
            current_recipe=recipe_metadata(session.current_recipe),
        )
    if any(
        word in compact
        for word in (
            "解冻好了", "解冻了", "已经解冻", "完全解冻", "新鲜", "鲜肉", "鲜鱼", "不是冷冻",
        )
    ):
        return begin_cooking(session)
    return session._result(
        WAITING_MEAT_THAW,
        True,
        feedback(
            "请告诉我食材已经解冻、是新鲜食材，还是还没解冻。",
            "请选择：已解冻 / 新鲜食材 / 还没解冻",
            robot_action="nod",
            led_effect="yellow",
            expression="curious",
            question=True,
        ),
    )


def begin_cooking(session: Any) -> dict[str, Any]:
    assert session.current_recipe is not None
    session.state, session.step_index = COOKING, 0
    ingredients = "、".join(
        ingredient_display(item) for item in session.current_recipe["ingredients"]
    )
    items = [
        feedback(
            f"好的，现在开始做{session.current_recipe['name']}。我们一步一步来。",
            f"开始：{session.current_recipe['name']}",
            robot_action="encourage_gesture",
            led_effect="green",
            expression="confident",
        )
    ]
    cache_name = session._new_cache_filename()
    if cache_name:
        count = session._cache_candidate_count()
        suffix = f"（同一文件含 {count} 个候选）" if count > 1 else ""
        items.append(
            feedback(
                f"这份完整菜谱已经保存{suffix}，下次相同需求可以直接使用。",
                f"菜谱已保存：{cache_name}{suffix}",
                robot_action="nod",
                led_effect="green_dynamic",
                expression="happy",
            )
        )
    items.extend(
        (
            feedback(
                f"先准备：{ingredients}。食材和调料都放在手边会更从容。",
                f"食材：{ingredients}",
                robot_action="nod",
                led_effect="warm_white",
                expression="focused",
            ),
            session._current_step_feedback(),
        )
    )
    return session._result(
        COOKING,
        True,
        *items,
        current_recipe=recipe_metadata(session.current_recipe),
        current_step=1,
    )
