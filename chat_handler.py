from __future__ import annotations

from typing import Any


def handle_chat(user_text: str) -> dict[str, Any]:
    if "你是谁" in user_text or "你叫什么" in user_text:
        speech = "我是机器狗助手，可以陪你聊天、执行简单动作，也可以通过 Skill 完成一些具体任务。"
    else:
        speech = "我在，你可以继续和我说。"

    return {
        "speech": speech,
        "display": speech,
        "robot_action": "idle_wait",
        "led_effect": "white",
        "expression": "neutral",
    }
