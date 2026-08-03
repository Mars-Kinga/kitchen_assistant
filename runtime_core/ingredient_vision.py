from __future__ import annotations

import re
import time
from typing import Any, Protocol

from .mac_camera import MacCameraError


class Camera(Protocol):
    def capture_data_url(self) -> str: ...


class VisionClient(Protocol):
    def is_available(self) -> bool: ...
    def vision_json(self, image_data_url: str, prompt: str) -> dict[str, Any]: ...


_VISUAL_MARKERS = ("帮我看看", "看一下", "看一看", "识别", "摄像头", "拍照")
_DEICTIC_MARKERS = (
    "这是什么", "这个是什么", "这是", "这个是", "这个食材", "这道菜", "镜头里的",
)
_DIRECT_QUESTIONS = ("这是什么", "这个是什么", "这道菜是什么", "这是什么食材")
_FOOD_MARKERS = (
    "食材", "菜", "葱", "蒜", "姜", "椒", "菇", "菌", "肉", "鱼", "虾", "蟹",
    "蛋", "豆", "瓜", "果", "蔬菜", "水果", "叶", "根", "茎",
)


def is_visual_identification_request(text: str) -> bool:
    compact = re.sub(r"\s+", "", str(text or ""))
    if not compact or not any(marker in compact for marker in _FOOD_MARKERS):
        return False
    explicit = any(marker in compact for marker in _VISUAL_MARKERS)
    deictic = any(marker in compact for marker in _DEICTIC_MARKERS)
    direct = any(marker in compact for marker in _DIRECT_QUESTIONS)
    comparison = "还是" in compact and deictic
    return direct or comparison or (explicit and deictic)


class IngredientVisionService:
    """Coordinate one camera frame and one short Qwen VL request."""

    def __init__(self, camera: Camera, client: VisionClient) -> None:
        self.camera = camera
        self.client = client

    def recognize(self, user_text: str) -> dict[str, Any]:
        if not self.client.is_available():
            return _error_result(
                "视觉识别还没有配置。请先设置 DASHSCOPE_API_KEY。",
                "视觉识别未配置",
            )

        started = time.perf_counter()
        try:
            image_data_url = self.camera.capture_data_url()
            captured = time.perf_counter()
            payload = self.client.vision_json(
                image_data_url,
                _vision_prompt(user_text),
            )
            finished = time.perf_counter()
            result = _normalize_result(payload)
        except MacCameraError as exc:
            return _error_result(str(exc), "摄像头不可用")
        except Exception:
            return _error_result(
                "图片识别暂时失败了，请保持食材不动后再试一次。",
                "视觉识别失败｜请重试",
            )

        answer = result["answer"]
        evidence = "、".join(result["visual_evidence"])
        if result["needs_retake"]:
            instruction = result["retake_instruction"] or "请靠近一些，补拍食材整体和根部。"
            speech = f"{answer}。目前还不能确定，{instruction}"
        else:
            speech = answer if not evidence else f"{answer}。我看到的依据是{evidence}。"
        display = answer
        if result["confidence_level"]:
            display += f"\n把握：{result['confidence_level']}"
        if evidence:
            display += f"\n依据：{evidence}"
        if result["needs_retake"]:
            display += f"\n补拍：{result['retake_instruction'] or '请补拍整体和根部'}"

        return {
            "route": "vision_result",
            "task_name": "食材视觉识别",
            "session_active": False,
            "speech": speech,
            "display": display,
            "robot_action": "nod",
            "led_effect": "green",
            "expression": "focused",
            "vision_result": result,
            "latency_ms": {
                "camera": round((captured - started) * 1000),
                "model": round((finished - captured) * 1000),
                "total": round((finished - started) * 1000),
            },
        }


def _vision_prompt(user_text: str) -> str:
    return (
        "你是厨房食材视觉识别器。根据图片回答用户问题，只识别可见食材或菜品；"
        "不能确认成熟度、过敏原或食品安全。无法可靠区分时必须要求补拍，不要猜。"
        "只返回合法JSON，不要Markdown或解释。"
        '结构：{"answer":string,"candidates":[string],'
        '"confidence_level":"高"|"中"|"低","visual_evidence":[string],'
        '"needs_retake":boolean,"retake_instruction":string|null}。'
        f"用户问题：{str(user_text).strip()[:200]}"
    )


def _normalize_result(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("视觉结果必须是对象")
    answer = str(payload.get("answer") or "").strip()
    if not answer or len(answer) > 160:
        raise ValueError("视觉回答为空或过长")
    candidates = _short_strings(payload.get("candidates"), limit=5, max_length=30)
    evidence = _short_strings(payload.get("visual_evidence"), limit=4, max_length=60)
    confidence = str(payload.get("confidence_level") or "").strip()
    if confidence not in {"高", "中", "低"}:
        confidence = "低"
    needs_retake = bool(payload.get("needs_retake")) or confidence == "低"
    instruction_raw = payload.get("retake_instruction")
    instruction = str(instruction_raw).strip()[:120] if instruction_raw else None
    return {
        "answer": answer,
        "candidates": candidates,
        "confidence_level": confidence,
        "visual_evidence": evidence,
        "needs_retake": needs_retake,
        "retake_instruction": instruction,
    }


def _short_strings(value: Any, *, limit: int, max_length: int) -> list[str]:
    if not isinstance(value, list):
        return []
    rows: list[str] = []
    for item in value[:limit]:
        text = str(item).strip()
        if text:
            rows.append(text[:max_length])
    return rows


def _error_result(speech: str, display: str) -> dict[str, Any]:
    return {
        "route": "vision_result",
        "task_name": "食材视觉识别",
        "session_active": False,
        "speech": speech,
        "display": display,
        "robot_action": "show_concern",
        "led_effect": "yellow",
        "expression": "alert",
    }
