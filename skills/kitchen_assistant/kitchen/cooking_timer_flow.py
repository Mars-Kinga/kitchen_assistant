from __future__ import annotations

from typing import Any

from .conversation_intents import is_affirmative
from .ingredient_vocabulary import CONCRETE_SEASONING_TERMS
from .intent_parser import extract_timer_seconds, is_likely_step_completion
from .parallel_prep import (
    find_parallel_prep_candidate,
    is_safe_water_boiling_prep,
    is_waiting_prep_instruction,
)
from .response_builder import feedback
from .states import COOKING
from .timer_controller import Timer, format_seconds, step_timer_hint


FREE_WAIT_DURING_TIMER_HINT = "在这段时间里你可以同步做自己想做的事情，时间到了我会叫你～"


def handle_timer(session: Any, text: str) -> dict[str, Any] | None:
    if "取消计时" in text:
        session.timer = None
        session.pending_timer_skip_confirmation = False
        return session._result(
            session.state,
            True,
            feedback(
                "计时已取消。", "计时已取消",
                robot_action="stop", led_effect="white", expression="neutral",
            ),
        )
    if session._has(text, "继续计时", "继续倒计时", "不要结束"):
        if session.timer is None:
            return session._result(
                session.state,
                True,
                feedback(
                    "当前没有正在运行的计时。", "暂无计时",
                    robot_action="nod", led_effect="white", expression="neutral",
                ),
            )
        return timer_still_running_response(session)
    seconds = extract_timer_seconds(text)
    if seconds is not None:
        session.timer = Timer(
            session.clock() + seconds,
            seconds,
            label="烹饪",
            step_index=session.step_index,
        )
        return session._result(
            session.state,
            True,
            feedback(
                f"已开始计时 {format_seconds(seconds)}。",
                f"倒计时：{format_seconds(seconds)}",
                robot_action="nod", led_effect="blue", expression="focused",
            ),
        )
    if session._has(text, "还有多久", "计时结束了吗"):
        if session.timer is None:
            return session._result(
                session.state,
                True,
                feedback(
                    "当前没有正在运行的计时。", "暂无计时",
                    robot_action="nod", led_effect="white", expression="neutral",
                ),
            )
        remaining = session._remaining_seconds() or 0
        return session._result(
            session.state,
            True,
            feedback(
                f"还剩 {format_seconds(remaining)}。",
                f"计时剩余：{format_seconds(remaining)}",
                robot_action="nod", led_effect="blue", expression="focused",
            ),
        )
    return None


def timer_end_if_due(session: Any) -> dict[str, Any] | None:
    if session.timer and session.timer.paused_remaining_seconds is not None:
        return None
    if not session.timer or session.clock() < session.timer.deadline:
        return None
    timer = session.timer
    session.timer = None
    session.pending_timer_skip_confirmation = False
    if timer.step_index is not None and timer.step_index == session.step_index and session.current_recipe:
        session._timed_step_ready_for_completion = session.step_index
        step = session._step()
        offered_index = session.parallel_offer_by_timer_step.get(timer.step_index)
        if (
            offered_index is not None
            and offered_index in session.offered_parallel_step_indexes
            and is_waiting_prep_instruction(str(step.get("instruction", "")))
        ):
            session.pending_parallel_timer_check_index = offered_index
            parallel_instruction = str(session.current_recipe["steps"][offered_index]["instruction"])
            return session._result(
                COOKING,
                True,
                feedback(
                    f"第 {session.step_index + 1} 步计时结束，请先检查当前腌制/浸泡状态。"
                    f"刚才计时时，你已经同步完成了“{parallel_instruction}”这一步，对吧？"
                    "请说“对”或“还没完成”。",
                    "计时结束｜并行准备完成了吗？",
                    robot_action="nod", led_effect="yellow", expression="focused",
                ),
                current_step=session.step_index + 1,
            )
        action = str(step.get("timer_end_action", ""))
        if action == "await_confirmation":
            session.pending_step_confirmation = step
            return session._result(
                COOKING,
                True,
                feedback(
                    str(step.get("timer_end_speech", "计时结束，请完成下一项确认操作。")),
                    str(step.get("timer_end_display", "计时结束｜等待确认")),
                    robot_action="show_concern", led_effect="yellow", expression="alert",
                ),
                current_step=session.step_index + 1,
            )
        if action == "advance":
            if session.step_index < len(session.current_recipe["steps"]) - 1:
                session.step_index += 1
            return session._result(
                COOKING,
                True,
                feedback(
                    str(step.get("timer_end_speech", "计时结束，请继续下一步。")),
                    str(step.get("timer_end_display", "计时结束｜进入下一步")),
                    robot_action="nod", led_effect="green_dynamic", expression="focused",
                ),
                session._current_step_feedback(),
                current_step=session.step_index + 1,
            )
    return session._result(
        session.state,
        True,
        timer_completion_feedback(session, timer),
        current_step=session.step_index + 1,
    )


def timer_completion_feedback(session: Any, timer: Timer) -> dict[str, str]:
    step = session._step() if session.current_recipe else {}
    instruction = str(step.get("instruction") or "").strip()
    if is_waiting_prep_instruction(instruction):
        speech = (
            f"计时结束，{format_seconds(timer.seconds)}的准备时间到了。可以检查腌制/浸泡状态；"
            "如果已经完成，就告诉我“做好了”，我再进入下一步。"
        )
    elif any(word in instruction for word in ("肉", "排骨", "鸡", "鱼", "虾")):
        speech = (
            f"计时结束，{format_seconds(timer.seconds)}下限时间到了。请检查食材是否完全变色、中心无粉红；"
            "如果还没达到，就继续中火翻炒，达到后告诉我“做好了”。"
        )
    else:
        speech = (
            f"计时结束，{format_seconds(timer.seconds)}参考时间到了。请按步骤中的状态检查食材；"
            "达到后告诉我“做好了”，不要只按时间判断。"
        )
    return feedback(
        speech,
        f"计时结束｜请检查：{instruction}\n完成后说：做好了",
        robot_action="show_concern", led_effect="yellow", expression="warning",
    )


def start_step_timer(session: Any, label: str | None = None) -> dict[str, str] | None:
    step = session._step()
    seconds = step.get("duration_seconds")
    if not isinstance(seconds, (int, float)) or seconds <= 0:
        return None
    seconds = int(seconds)
    session._timed_step_ready_for_completion = None
    if label is None:
        label = str(step.get("timer_label") or f"第 {session.step_index + 1} 步")
    session.timer = Timer(
        session.clock() + seconds,
        seconds,
        label=label,
        step_index=session.step_index,
    )
    parallel_candidate = (
        parallel_prep_candidate(session)
        if is_waiting_prep_instruction(str(step.get("instruction", "")))
        else None
    )
    if parallel_candidate is not None:
        parallel_index, _ = parallel_candidate
        session.offered_parallel_step_indexes.add(parallel_index)
        session.parallel_offer_by_timer_step[session.step_index] = parallel_index
    parallel_hint = parallel_prep_hint(session, parallel_candidate)
    speech = f"好的，{label}开始计时 {format_seconds(seconds)}。我会在时间到时提醒你。"
    display = f"{label}倒计时：{format_seconds(seconds)}"
    if parallel_hint:
        speech = f"{speech} {parallel_hint}"
        display = f"{display}\n{parallel_hint}"
    return feedback(
        speech,
        display,
        robot_action="nod", led_effect="green_dynamic", expression="focused",
    )


def request_early_timer_end(session: Any) -> dict[str, Any]:
    session.pending_timer_skip_confirmation = True
    remaining = format_seconds(session._remaining_seconds() or 0)
    return session._result(
        COOKING,
        True,
        feedback(
            f"现在第 {session.step_index + 1} 步正在计时，还剩约 {remaining}。"
            "确定要结束计时并前往下一步吗？请说“确认”或“继续计时”。",
            "计时进行中｜确认结束计时？",
            robot_action="show_concern", led_effect="yellow", expression="alert",
        ),
        current_step=session.step_index + 1,
    )


def handle_timer_skip_confirmation(session: Any, text: str) -> dict[str, Any]:
    compact = text.replace(" ", "")
    if is_affirmative(compact):
        session.pending_timer_skip_confirmation = False
        session.timer = None
        prefix = feedback(
            "好的，已按你的确认提前结束计时，进入下一步。",
            "已结束计时｜进入下一步",
            robot_action="nod", led_effect="green_dynamic", expression="focused",
        )
        return session._with_prefix(prefix, session._advance_after_completion())
    if session._has(compact, "继续计时", "不要", "不结束", "继续等", "取消"):
        session.pending_timer_skip_confirmation = False
        return timer_still_running_response(session)
    return session._result(
        COOKING,
        True,
        feedback(
            "当前计时仍在进行。请说“确认”提前结束并前往下一步，或说“继续计时”。",
            "等待确认｜结束计时？",
            robot_action="nod", led_effect="yellow", expression="alert",
        ),
        current_step=session.step_index + 1,
    )


def current_step_has_unfinished_timer(session: Any) -> bool:
    seconds = session._step().get("duration_seconds")
    return (
        isinstance(seconds, (int, float))
        and seconds > 0
        and session.timer is None
        and session._timed_step_ready_for_completion != session.step_index
    )


def request_unstarted_timer_confirmation(session: Any) -> dict[str, Any]:
    session.pending_unstarted_timer_confirmation = True
    seconds = int(session._step()["duration_seconds"])
    return session._result(
        COOKING,
        True,
        feedback(
            f"当前第 {session.step_index + 1} 步建议计时 {format_seconds(seconds)}，但计时还没有启动。"
            "如果你已经自行计时并检查完成，请说“确认完成”；要使用助手计时，请说“开始计时”。",
            "当前步骤尚未计时｜确认完成 / 开始计时",
            robot_action="show_concern", led_effect="yellow", expression="alert",
        ),
        current_step=session.step_index + 1,
    )


def handle_unstarted_timer_confirmation(session: Any, text: str) -> dict[str, Any]:
    compact = text.replace(" ", "")
    if session._has(compact, "开始计时", "给我计时", "启动计时"):
        session.pending_unstarted_timer_confirmation = False
        timer_feedback = start_step_timer(session)
        assert timer_feedback is not None
        return session._result(COOKING, True, timer_feedback, current_step=session.step_index + 1)
    if session._has(compact, "确认完成", "已经计时并完成", "自行计时完成", "我自己计时了"):
        session.pending_unstarted_timer_confirmation = False
        return session._advance_after_completion()
    if session._has(compact, "取消", "继续当前步骤", "还没完成", "不跳过"):
        session.pending_unstarted_timer_confirmation = False
        return session._result(
            COOKING,
            True,
            session._current_step_feedback("好的，继续当前步骤："),
            current_step=session.step_index + 1,
        )
    return session._result(
        COOKING,
        True,
        feedback(
            "请说“确认完成”表示你已自行计时并检查完成，或说“开始计时”使用助手计时。",
            "等待确认｜确认完成 / 开始计时",
            robot_action="nod", led_effect="yellow", expression="alert",
        ),
        current_step=session.step_index + 1,
    )


def handle_parallel_step_confirmation(session: Any, text: str) -> dict[str, Any]:
    assert session.pending_parallel_step_index is not None
    compact = text.replace(" ", "")
    parallel_index = session.pending_parallel_step_index
    if session._has(compact, "还没", "没有", "不对", "没做", "没完成"):
        session.completed_parallel_step_indexes.discard(parallel_index)
        session.offered_parallel_step_indexes.discard(parallel_index)
        session.pending_parallel_step_index = None
        return session._result(
            COOKING, True, session._current_step_feedback("好的，那我们现在完成："),
            current_step=session.step_index + 1,
        )
    if is_affirmative(compact):
        session.completed_parallel_step_indexes.add(parallel_index)
        session.offered_parallel_step_indexes.discard(parallel_index)
        session.pending_parallel_step_index = None
        return session._advance_after_completion()
    return session._result(
        COOKING,
        True,
        feedback(
            "刚才计时期间这一步已记录为完成。请说“对”跳过它，或说“还没完成”现在来做。",
            "等待确认｜并行准备是否已完成？",
            robot_action="nod", led_effect="warm_white", expression="focused",
        ),
        current_step=session.step_index + 1,
    )


def handle_parallel_timer_check(session: Any, text: str) -> dict[str, Any]:
    assert session.pending_parallel_timer_check_index is not None
    parallel_index = session.pending_parallel_timer_check_index
    compact = text.replace(" ", "")
    if session._has(compact, "还没", "没有", "不对", "没做", "没完成"):
        session.offered_parallel_step_indexes.discard(parallel_index)
        session.pending_parallel_timer_check_index = None
        return session._result(
            COOKING,
            True,
            feedback(
                "好的，先不用做并行准备。请检查当前计时步骤，确认完成后告诉我“做好了”。",
                "并行准备未完成｜继续当前步骤",
                robot_action="nod", led_effect="warm_white", expression="focused",
            ),
            current_step=session.step_index + 1,
        )
    if is_affirmative(compact):
        session.completed_parallel_step_indexes.add(parallel_index)
        session.offered_parallel_step_indexes.discard(parallel_index)
        session.pending_parallel_timer_check_index = None
        return session._result(
            COOKING,
            True,
            feedback(
                "好的，我已记下这项并行准备完成。请检查当前计时步骤；确认腌制/浸泡也完成后，"
                "告诉我“做好了”，我会跳过这项已完成的准备。",
                "已记录并行准备｜继续检查当前步骤",
                robot_action="nod", led_effect="green_dynamic", expression="focused",
            ),
            current_step=session.step_index + 1,
        )
    return session._result(
        COOKING,
        True,
        feedback(
            "刚才计时时的并行准备是否完成？请说“对”或“还没完成”。",
            "等待确认｜并行准备完成了吗？",
            robot_action="nod", led_effect="yellow", expression="focused",
        ),
        current_step=session.step_index + 1,
    )


def timer_still_running_response(session: Any) -> dict[str, Any]:
    return session._result(
        COOKING,
        True,
        feedback(
            "当前步骤的计时还没结束，先继续观察和操作；计时结束后检查状态，再告诉我“做好了”。",
            f"计时进行中｜剩余约 {format_seconds(session._remaining_seconds() or 0)}",
            robot_action="nod", led_effect="blue_dynamic", expression="focused",
        ),
        current_step=session.step_index + 1,
    )


def current_step_feedback(session: Any, prefix: str = "") -> dict[str, str]:
    step = session._step()
    instruction = step["instruction"]
    timer_hint = step_timer_hint(step)
    parallel_hint = parallel_prep_hint(session) if timer_is_running_for_current_step(session) else None
    if timer_hint:
        instruction = f"{instruction} {timer_hint}"
    if parallel_hint:
        instruction = f"{instruction} {parallel_hint}"
    if step.get("safety_note"):
        instruction += f" 注意：{step['safety_note']}"
    display = step["display_text"]
    if step.get("safety_note"):
        display = f"{display}\n注意：{step['safety_note']}"
    if timer_hint:
        display = f"{display}\n{timer_hint}"
    if parallel_hint:
        display = f"{display}\n{parallel_hint}"
    return feedback(
        f"{prefix}{instruction}",
        display,
        robot_action=step["robot_action"],
        led_effect=step["led_effect"],
        expression=step["expression"],
    )


def parallel_prep_candidate(session: Any) -> tuple[int, str] | None:
    if not session.current_recipe:
        return None
    ingredient_names = [
        str(item.get("name", "")).strip()
        for item in session.current_recipe.get("ingredients", [])
        if str(item.get("name", "")).strip()
    ]
    return find_parallel_prep_candidate(
        list(session.current_recipe.get("steps", [])),
        session.step_index,
        session.completed_parallel_step_indexes,
        ingredient_names,
    )


def parallel_prep_hint(
    session: Any,
    candidate: tuple[int, str] | None = None,
) -> str | None:
    if not session.current_recipe:
        return None
    instruction = str(session._step().get("instruction", ""))
    if not is_waiting_prep_instruction(instruction):
        return None
    candidate = candidate if candidate is not None else parallel_prep_candidate(session)
    if candidate:
        _, candidate_text = candidate
        if is_safe_water_boiling_prep(candidate_text):
            return (
                f"等待的时候可以按照时间规划，同步做这一步：{candidate_text}。"
                "这一步只烧清水，不接触正在腌制的肉；水沸后说“准备好了”，当前腌制计时不会被打断。"
            )
        return (
            f"等待期间可以先按照你的时间规划准备下一步：{candidate_text}。"
            "只做不动火的准备，不要提前热油或开火；当前计时不会被打断。"
        )
    return FREE_WAIT_DURING_TIMER_HINT


def timer_is_running_for_current_step(session: Any) -> bool:
    return session.timer is not None and session.timer.step_index == session.step_index


def is_parallel_prep_ack(session: Any, text: str) -> bool:
    if not session.current_recipe or not timer_is_running_for_current_step(session):
        return False
    instruction = str(session._step().get("instruction", ""))
    if not is_waiting_prep_instruction(instruction):
        return False
    compact = text.replace(" ", "")
    if any(word in compact for word in ("腌制好了", "腌好了", "浸泡好了", "时间到了", "计时结束")):
        return False
    if any(marker in compact for marker in ("吗", "？", "?", "怎么", "如何")):
        return False
    candidate = parallel_prep_candidate(session)
    if candidate is None:
        return False
    _, candidate_text = candidate
    if any(word in compact for word in ("准备好了", "调料好了", "配料好了", "食材好了")):
        return True
    prep_terms = (
        "糖醋汁", "调味汁", "酱汁", "调料", "配料", "小碗",
        *CONCRETE_SEASONING_TERMS,
        "清水", "水", "烧开", "沸腾",
    )
    mentioned_terms = [term for term in prep_terms if term in candidate_text]
    return bool(mentioned_terms) and is_likely_step_completion(text) and any(
        term in compact for term in mentioned_terms
    )
