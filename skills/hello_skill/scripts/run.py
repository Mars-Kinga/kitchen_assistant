from __future__ import annotations

from typing import Any


def run(arguments: dict[str, Any]) -> dict[str, Any]:
    user_text = arguments.get("user_text", "")
    return {
        "route": "skill_result",
        "task_name": "问候",
        "speech": f"你好，我收到了你的输入：{user_text}。这是一个示例 Skill 的回复。",
        "display": "示例 Skill 已运行",
        "fallback": "如果无法展示文字，则只进行语音播报。"
    }
