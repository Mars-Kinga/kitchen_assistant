from __future__ import annotations

from typing import Any


DEFAULTS = {
    "robot_action": "nod",
    "led_effect": "white",
    "expression": "neutral",
}


def feedback(
    speech: str,
    display: str,
    *,
    robot_action: str = DEFAULTS["robot_action"],
    led_effect: str = DEFAULTS["led_effect"],
    expression: str = DEFAULTS["expression"],
    question: bool = False,
) -> dict[str, str]:
    """Build the one normalized five-channel feedback shape for this Skill."""
    result = {
        "display": str(display) if display else str(speech)[:60],
        "robot_action": str(robot_action or DEFAULTS["robot_action"]),
        "led_effect": str(led_effect or DEFAULTS["led_effect"]),
        "expression": str(expression or DEFAULTS["expression"]),
    }
    result["question" if question else "speech"] = str(speech) or "请再说一次。"
    return result


def result(state: str, active: bool, *items: dict[str, Any], **metadata: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "route": "skill_result",
        "task_name": "AI 厨房助手",
        "kitchen_state": state,
        "session_active": active,
    }
    if len(items) == 1:
        payload.update(items[0])
    else:
        payload["steps"] = list(items)
    payload.update(metadata)
    return payload
