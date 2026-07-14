from __future__ import annotations

from typing import Any


SUPPORTED_ACTIONS = {
    "idle_wait", "speak", "wave_hand", "handshake", "fist_bump", "high_five",
    "nod", "shake_head", "show_smile", "show_concern", "encourage_gesture", "hug",
    "turn_left", "turn_right", "step_forward", "step_back", "stop", "breathing_guide", "goodbye",
}
SUPPORTED_EFFECTS = {"off", "white", "blue", "green", "yellow", "red", "warm_white", "green_dynamic", "blue_dynamic", "rainbow"}
SUPPORTED_EXPRESSIONS = {"neutral", "happy", "curious", "focused", "confident", "alert", "waiting", "confused", "excited", "warning"}


def enrich_step(step: dict[str, Any], index: int, total: int) -> dict[str, Any]:
    """Fill feedback fields with actions supported by the fixed mock SDK."""
    instruction = str(step.get("instruction", "")).strip()
    lowered = instruction.lower()
    if any(word in lowered for word in ("切", "切块", "切丝")):
        action = "show_concern"
    elif any(word in lowered for word in ("搅拌", "打散", "搅匀", "翻炒", "翻面", "炒")):
        action = "encourage_gesture"
    elif any(word in lowered for word in ("倒入", "加入", "放入")):
        action = "nod"
    elif any(word in lowered for word in ("完成", "装盘")):
        action = "high_five" if "完成" in lowered else "nod"
    else:
        action = "nod"

    caution = any(word in lowered for word in ("热油", "热锅", "刀", "沸", "火"))
    complete = any(word in lowered for word in ("完成", "装盘", "关火"))
    if complete:
        led, expression = "rainbow", "excited"
    elif caution:
        led, expression = "yellow", "warning"
    elif any(word in lowered for word in ("翻炒", "炒", "搅拌", "打散")):
        led, expression = "green_dynamic", "happy"
    elif any(word in lowered for word in ("调味", "盐", "生抽", "葱花", "香菜")):
        led, expression = "warm_white", "focused"
    else:
        led, expression = "blue_dynamic", "focused"
    supplied_action = str(step.get("robot_action") or "")
    supplied_effect = str(step.get("led_effect") or "")
    supplied_expression = str(step.get("expression") or "")
    return {
        "step_number": index,
        "instruction": instruction,
        "display_text": str(step.get("display_text") or f"步骤 {index}/{total}：{instruction[:18]}"),
        "duration_seconds": step.get("duration_seconds"),
        "heat_level": step.get("heat_level"),
        "robot_action": supplied_action if supplied_action in SUPPORTED_ACTIONS else action,
        "led_effect": supplied_effect if supplied_effect in SUPPORTED_EFFECTS else led,
        "expression": supplied_expression if supplied_expression in SUPPORTED_EXPRESSIONS else expression,
        "safety_note": step.get("safety_note"),
    }
