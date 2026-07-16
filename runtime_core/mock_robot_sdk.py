from __future__ import annotations

from typing import Any


SUPPORTED_CAPABILITIES = frozenset({"voice", "display", "motion", "light", "expression"})


class MockMotionSDK:
    """Simulated robot motion SDK.

    This class does not control real hardware. It only prints clear runtime logs so
    Skill developers can verify that their structured output would trigger the
    expected robot capability.
    """

    ACTION_TEXT = {
        "idle_wait": "原地等待",
        "speak": "语音播报",
        "wave_hand": "挥手",
        "handshake": "握手",
        "fist_bump": "碰拳",
        "high_five": "击掌",
        "nod": "点头",
        "shake_head": "摇头",
        "show_smile": "微笑表情",
        "show_concern": "关心表情",
        "encourage_gesture": "鼓励手势",
        "hug": "拥抱",
        "turn_left": "左转",
        "turn_right": "右转",
        "step_forward": "前进一步",
        "step_back": "后退一步",
        "stop": "停止运动",
        "breathing_guide": "呼吸引导动作",
        "goodbye": "送别动作",
    }

    def execute(self, action: str | None, **kwargs: Any) -> None:
        action = action or "idle_wait"
        text = self.ACTION_TEXT.get(action, action)
        if kwargs:
            print(f"[模拟SDK-动作] {text} | action={action} | params={kwargs}")
        else:
            print(f"[模拟SDK-动作] {text} | action={action}")


class MockLightSDK:
    """Simulated LED/light-strip SDK."""

    EFFECT_TEXT = {
        "off": "关闭灯带",
        "white": "白色常亮",
        "blue": "蓝色常亮",
        "green": "绿色常亮",
        "yellow": "黄色常亮",
        "red": "红色常亮",
        "warm_white": "暖白低亮",
        "green_dynamic": "绿色动态效果",
        "blue_dynamic": "蓝色动态效果",
        "rainbow": "彩色庆祝效果",
    }

    def set_effect(self, effect: str | None) -> None:
        if not effect:
            return
        text = self.EFFECT_TEXT.get(effect, effect)
        print(f"[模拟SDK-灯带] {text} | effect={effect}")


class MockDisplaySDK:
    """Simulated screen/display SDK."""

    def show_text(self, text: str | None) -> None:
        if text:
            print(f"[模拟SDK-屏幕] {text}")

    def show_expression(self, expression: str | None) -> None:
        if expression:
            print(f"[模拟SDK-表情] {expression}")


class MockRobotSDK:
    """Aggregates simulated robot capabilities used by RuntimeExecutor."""

    def __init__(self) -> None:
        self.motion = MockMotionSDK()
        self.light = MockLightSDK()
        self.display = MockDisplaySDK()
