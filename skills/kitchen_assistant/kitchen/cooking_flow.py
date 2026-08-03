from __future__ import annotations

from typing import Any

from .ingredient_answers import answer_ingredient_list
from .intent_parser import is_likely_next_step, is_likely_step_completion, is_likely_timer_start
from .models import CookingContext
from .parallel_prep import is_waiting_prep_instruction
from .response_builder import feedback
from .response_phrases import (
    FINISHED_RESPONSES,
    STEP_ENCOURAGEMENTS,
    WAITING_ACKNOWLEDGMENTS,
)
from .session_presenter import recipe_metadata
from .states import COMPLETED, COOKING, PAUSED
from .timer_controller import signals_step_timer_start


def handle_cooking_turn(session: Any, text: str) -> dict[str, Any]:
    ingredient_answer = answer_ingredient_list(session, text, state=COOKING)
    if ingredient_answer:
        return ingredient_answer
    if session.pending_parallel_timer_check_index is not None:
        return session._handle_parallel_timer_check(text)
    if session.pending_timer_skip_confirmation:
        return session._handle_timer_skip_confirmation(text)
    if session.pending_unstarted_timer_confirmation:
        return session._handle_unstarted_timer_confirmation(text)
    if session.pending_parallel_step_index is not None:
        return session._handle_parallel_step_confirmation(text)
    if session.pending_step_confirmation:
        markers = tuple(
            str(marker)
            for marker in session.pending_step_confirmation.get("confirmation_markers", [])
            if str(marker)
        )
        if markers and session._has(text, *markers):
            confirmation = session.pending_step_confirmation
            session.pending_step_confirmation = None
            if session.step_index < len(session.current_recipe["steps"]) - 1:
                session.step_index += 1
            step_feedback = session._current_step_feedback(
                str(confirmation.get("confirmation_prefix", "确认完成。"))
            )
            timer_feedback = session._start_step_timer()
            items = [step_feedback]
            if timer_feedback:
                items.append(timer_feedback)
            return session._result(COOKING, True, *items, current_step=session.step_index + 1)
        return session._result(
            COOKING,
            True,
            feedback(
                str(session.pending_step_confirmation.get("waiting_speech", "请先完成当前确认操作后再继续。")),
                str(session.pending_step_confirmation.get("waiting_display", "等待确认")),
                robot_action="show_concern",
                led_effect="yellow",
                expression="alert",
            ),
            current_step=session.step_index + 1,
        )
    if session._has(text, "暂停", "先等一下"):
        if session.timer is not None:
            session.timer.paused_remaining_seconds = session._remaining_seconds()
        session.state = PAUSED
        return session._result(
            PAUSED,
            True,
            feedback(
                "好的，烹饪指导已暂停。",
                "烹饪已暂停",
                robot_action="stop",
                led_effect="yellow",
                expression="waiting",
            ),
        )
    if session._has(text, "再说一遍", "重复一下"):
        return session._result(
            COOKING,
            True,
            session._current_step_feedback("重复："),
            current_step=session.step_index + 1,
        )
    if session._has(text, "我做到哪一步", "现在做到哪一步", "做到哪一步", "当前是什么步骤", "当前步骤"):
        step = session._step()
        return session._result(
            COOKING,
            True,
            feedback(
                f"你现在在第 {session.step_index + 1}/{len(session.current_recipe['steps'])} 步。{step['instruction']}",
                step["display_text"],
                robot_action="nod",
                led_effect="blue",
                expression="focused",
            ),
            current_step=session.step_index + 1,
        )
    if session._has(text, "上一步"):
        if session.step_index == 0:
            return session._result(
                COOKING,
                True,
                feedback(
                    "当前已经是第一步，我为你重复一次。",
                    session._step()["display_text"],
                    robot_action="nod",
                    led_effect="blue",
                    expression="focused",
                ),
                current_step=1,
            )
        session.step_index -= 1
        return session._result(
            COOKING,
            True,
            session._current_step_feedback("返回："),
            current_step=session.step_index + 1,
        )
    if session._has(text, "下一步", "继续", "跳过", "结束计时", "提前结束") or is_likely_next_step(text):
        if session.timer is not None and session.timer.step_index == session.step_index:
            return session._request_early_timer_end()
        if session._current_step_has_unfinished_timer():
            return session._request_unstarted_timer_confirmation()
        return session._advance_after_completion()
    if session._has(text, "开始计时", "开始煎", "开始炒", "开始煮", "开始"):
        timer_feedback = session._start_step_timer()
        if timer_feedback:
            return session._result(COOKING, True, timer_feedback, current_step=session.step_index + 1)
    if signals_step_timer_start(text, session._step() if session.current_recipe else {}):
        timer_feedback = session._start_step_timer()
        if timer_feedback:
            return session._result(COOKING, True, timer_feedback, current_step=session.step_index + 1)
    if session._is_parallel_prep_ack(text):
        parallel = session._parallel_prep_candidate()
        assert parallel is not None
        parallel_index, parallel_text = parallel
        session.completed_parallel_step_indexes.add(parallel_index)
        session.offered_parallel_step_indexes.discard(parallel_index)
        return session._result(
            COOKING,
            True,
            feedback(
                f"记住了，已记录你完成了“{parallel_text}”；当前腌制/浸泡计时继续，不会提前进入下一步。",
                "辅助准备已记录｜当前计时继续",
                robot_action="nod",
                led_effect="warm_white",
                expression="focused",
            ),
            current_step=session.step_index + 1,
        )
    if (
        is_waiting_prep_instruction(str(session._step().get("instruction", "")))
        and session.timer is None
        and session._timed_step_ready_for_completion != session.step_index
        and (is_likely_timer_start(text) or is_likely_step_completion(text))
    ):
        timer_feedback = session._start_step_timer()
        if timer_feedback:
            return session._result(COOKING, True, timer_feedback, current_step=session.step_index + 1)
    if (
        session.timer is not None
        and session.timer.step_index == session.step_index
        and session._acknowledges_step_completion(text)
    ):
        return session._request_early_timer_end()
    if session._is_step_acknowledgment(text):
        return session._result(
            COOKING,
            True,
            feedback(
                session.phrases.choose("waiting_ack", WAITING_ACKNOWLEDGMENTS),
                "继续当前步骤｜完成后告诉我",
                robot_action="nod",
                led_effect="warm_white",
                expression="focused",
            ),
            current_step=session.step_index + 1,
        )
    if session._acknowledges_step_completion(text):
        if session._current_step_has_unfinished_timer():
            return session._request_unstarted_timer_confirmation()
        return session._advance_after_completion()
    answer = answer_question(session, text)
    if answer:
        return answer
    return session._result(
        COOKING,
        True,
        feedback(
            "我没有听懂。你可以问当前烹饪问题，或说下一步、上一步、暂停、计时、退出。",
            "可用命令：下一步 / 上一步 / 暂停 / 计时 / 退出",
            robot_action="nod",
            led_effect="white",
            expression="confused",
        ),
    )


def handle_paused_turn(session: Any, text: str) -> dict[str, Any]:
    ingredient_answer = answer_ingredient_list(session, text, state=PAUSED)
    if ingredient_answer:
        return ingredient_answer
    if session._has(text, "继续", "恢复", "继续做"):
        session.state = COOKING
        if session.timer is not None and session.timer.paused_remaining_seconds is not None:
            session.timer.deadline = session.clock() + session.timer.paused_remaining_seconds
            session.timer.paused_remaining_seconds = None
        return session._result(
            COOKING,
            True,
            feedback(
                "已恢复烹饪指导。",
                "已恢复当前步骤",
                robot_action="nod",
                led_effect="blue_dynamic",
                expression="focused",
            ),
            session._current_step_feedback("继续当前步骤："),
            current_step=session.step_index + 1,
        )
    answer = answer_question(session, text)
    if answer:
        return answer
    return session._result(
        PAUSED,
        True,
        feedback(
            "现在仍处于暂停状态。你可以说继续、询问问题、查询计时或退出。",
            "烹饪已暂停",
            robot_action="stop",
            led_effect="yellow",
            expression="waiting",
        ),
    )


def answer_question(session: Any, text: str) -> dict[str, Any] | None:
    if not session.current_recipe:
        return None
    context = CookingContext(
        recipe=session.current_recipe,
        current_step=session._step(),
        servings=session.request.servings,
        taste_preferences=session.request.taste_preferences,
        dietary_restrictions=session.request.dietary_restrictions,
        available_ingredients=session.request.available_ingredients,
        available_equipment=session.request.available_equipment,
        timer_remaining_seconds=session._remaining_seconds(),
        conversation_summary=session.conversation_summary[-5:],
    )
    if will_use_agent_for_question(session, text, context):
        session._emit_progress(
            feedback(
                "请稍后，我正在思考要怎么应对。",
                "请稍后，我正在思考要怎么应对",
                robot_action="nod",
                led_effect="blue_dynamic",
                expression="focused",
            )
        )
    answer = session.question_service.answer(text, context)
    if answer is None:
        return None
    session.conversation_summary.append(f"问：{text} 答：{answer.answer}")
    next_state = PAUSED if answer.should_pause_cooking else session.state
    if answer.should_pause_cooking:
        session.state = PAUSED
    answer_feedback = feedback(
        answer.answer,
        answer.display_text,
        robot_action=answer.robot_action,
        led_effect=answer.led_effect,
        expression=answer.expression,
    )
    if answer.safety_level == "NORMAL":
        answer_feedback.update(
            speech=f"我想一想，结合你现在这一步来判断。{answer.answer}",
            display=f"我在陪你一起判断\n{answer.display_text}",
            robot_action="nod",
            led_effect="green_dynamic",
            expression="focused",
        )
    return session._result(
        next_state,
        True,
        answer_feedback,
        current_step=session.step_index + 1,
        safety_level=answer.safety_level,
    )


def will_use_agent_for_question(session: Any, text: str, context: CookingContext) -> bool:
    client = getattr(session.question_service, "llm_client", None)
    is_available = getattr(client, "is_available", None)
    if not callable(is_available) or not is_available():
        return False
    return session.offline_question_service.answer(text, context) is None


def advance_after_completion(session: Any) -> dict[str, Any]:
    assert session.current_recipe is not None
    completed_number = session.step_index + 1
    session._timed_step_ready_for_completion = None
    if session.step_index >= len(session.current_recipe["steps"]) - 1:
        session.state = COMPLETED
        return session._result(
            COMPLETED,
            False,
            finished_feedback(session),
            current_recipe=recipe_metadata(session.current_recipe),
        )
    skipped_parallel_steps: list[int] = []
    while session.step_index < len(session.current_recipe["steps"]) - 1:
        session.step_index += 1
        if session.step_index in session.completed_parallel_step_indexes:
            skipped_parallel_steps.append(session.step_index)
            continue
        if session.step_index in session.offered_parallel_step_indexes:
            session.pending_parallel_step_index = session.step_index
            instruction = session._step()["instruction"]
            return session._result(
                COOKING,
                True,
                feedback(
                    f"刚才计时时，你已经同步完成了“{instruction}”这一步，对吧？说“对”就跳过它；"
                    "如果还没完成，请说“还没完成”。",
                    "并行准备确认｜刚才完成了吗？",
                    robot_action="nod",
                    led_effect="warm_white",
                    expression="focused",
                ),
                current_step=session.step_index + 1,
            )
        break
    if (
        session.step_index >= len(session.current_recipe["steps"]) - 1
        and session.step_index in session.completed_parallel_step_indexes
    ):
        session.state = COMPLETED
        return session._result(
            COMPLETED,
            False,
            finished_feedback(session),
            current_recipe=recipe_metadata(session.current_recipe),
        )
    encouragement = session.phrases.choose("step_encouragement", STEP_ENCOURAGEMENTS)
    skipped_text = ""
    if skipped_parallel_steps:
        numbers = "、".join(str(index + 1) for index in skipped_parallel_steps)
        skipped_text = f"已跳过计时期间确认完成的第 {numbers} 步。"
    return session._result(
        COOKING,
        True,
        feedback(
            f"{encouragement} 已完成第 {completed_number} 步。{skipped_text}"
            f"接下来进行第 {session.step_index + 1} 步。",
            f"已完成第 {completed_number} 步，进入第 {session.step_index + 1} 步",
            robot_action="encourage_gesture",
            led_effect="green_dynamic",
            expression="happy",
        ),
        session._current_step_feedback(),
        current_step=session.step_index + 1,
    )


def finished_feedback(session: Any) -> dict[str, str]:
    assert session.current_recipe is not None
    return feedback(
        session.phrases.choose("finished", FINISHED_RESPONSES).format(
            dish=session.current_recipe["name"]
        ),
        f"{session.current_recipe['name']}完成",
        robot_action="high_five",
        led_effect="rainbow",
        expression="excited",
    )


def acknowledges_step_completion(text: str) -> bool:
    return is_likely_step_completion(text)
