from __future__ import annotations

from typing import Any

from .mock_robot_sdk import MockRobotSDK
from .voice_io import VoiceOutput


class RuntimeExecutor:
    """Execute structured Skill output through simulated robot SDK.

    Current runtime is for local simulation only. It does not call real robot
    hardware. Motion, light strip, display and expression calls are printed as
    logs, while speech still goes through the configured TTS backend.
    """

    def __init__(
        self,
        no_play: bool = False,
        tts_backend: str = "edge",
        edge_voice: str = "zh-CN-XiaoxiaoNeural",
        edge_rate: str = "+8%",
    ) -> None:
        self.robot = MockRobotSDK()
        self.voice = VoiceOutput(
            tts_backend=tts_backend,
            edge_voice=edge_voice,
            edge_rate=edge_rate,
            no_play=no_play,
        )

    def execute_plan(self, plan: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(plan, dict):
            print("[执行器-警告] Skill 输出不是对象，已使用安全反馈。")
            plan = {}
        if isinstance(plan.get("steps"), list):
            executed = 0
            for index, step in enumerate(plan["steps"], start=1):
                print(f"\n[执行步骤 {index}] {step.get('stage', '')}")
                self._execute_single(step)
                executed += 1
            return {"status": "ok", "steps_executed": executed}

        self._execute_single(plan)
        return {
            "status": "ok",
            "robot_action": plan.get("robot_action", "idle_wait"),
            "speech_played": bool(plan.get("speech") or plan.get("question")),
            "display_rendered": bool(plan.get("display")),
            "led_effect": plan.get("led_effect"),
            "expression": plan.get("expression"),
        }

    def _execute_single(self, item: dict[str, Any]) -> None:
        if not isinstance(item, dict):
            print("[执行器-警告] 单条反馈不是对象，已安全降级。")
            item = {}

        action = self._as_supported_action(self._as_text(item.get("robot_action"), "idle_wait"))
        speech = self._as_text(item.get("speech") or item.get("question"), "暂时无法生成回复，请再试一次。")
        display = self._as_text(item.get("display"), speech[:60])
        led_effect = self._as_supported_effect(self._as_text(item.get("led_effect") or item.get("light_effect"), "white"))
        expression = self._as_supported_expression(self._as_text(item.get("expression"), "neutral"))

        # Keep independent simulated capabilities independent: one failed
        # capability must not hide the other four during a demo.
        self._safe_call("灯带", self.robot.light.set_effect, led_effect)
        self._safe_call("表情", self.robot.display.show_expression, expression)
        self._safe_call("动作", self.robot.motion.execute, action)
        self._safe_call("屏幕", self.robot.display.show_text, display)
        print(f"[模拟SDK-语音请求] {speech}")
        self._safe_call("语音", self.voice.speak, speech)

    @staticmethod
    def _as_text(value: Any, fallback: str) -> str:
        if value is None or value == "":
            print(f"[执行器-警告] 缺失反馈字段，已降级为：{fallback}")
            return fallback
        if not isinstance(value, str):
            print(f"[执行器-警告] 反馈字段类型无效，已转换为文本。")
            return str(value)
        return value

    def _as_supported_action(self, action: str) -> str:
        if action not in self.robot.motion.ACTION_TEXT:
            print(f"[执行器-警告] 未知动作，已降级为：idle_wait（收到：{action}）")
            return "idle_wait"
        return action

    def _as_supported_effect(self, effect: str) -> str:
        if effect not in self.robot.light.EFFECT_TEXT:
            print(f"[执行器-警告] 未知灯带效果，已降级为：white（收到：{effect}）")
            return "white"
        return effect

    @staticmethod
    def _as_supported_expression(expression: str) -> str:
        supported = {"neutral", "happy", "curious", "focused", "confident", "alert", "waiting", "confused", "excited", "warning"}
        if expression not in supported:
            print(f"[执行器-警告] 未知表情，已降级为：neutral（收到：{expression}）")
            return "neutral"
        return expression

    @staticmethod
    def _safe_call(capability: str, callback: Any, *args: Any) -> None:
        try:
            callback(*args)
        except Exception as exc:  # Mock extensions must not break the demo.
            print(f"[执行器-警告] {capability}模拟调用失败：{exc}")
