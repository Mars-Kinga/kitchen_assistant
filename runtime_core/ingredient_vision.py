from __future__ import annotations

import re
import threading
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
        self._capture_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._camera_state = "idle"
        self._camera_message = "尚未读取摄像头"
        self._last_capture_at: float | None = None

    def status_snapshot(self) -> dict[str, Any]:
        with self._state_lock:
            return {
                "state": self._camera_state,
                "message": self._camera_message,
                "last_capture_at": self._last_capture_at,
                "model_available": bool(self.client.is_available()),
            }

    def capture_preview(self) -> str:
        """Capture one frame for the console without persisting it."""
        with self._capture_lock:
            self._set_camera_state("capturing", "正在读取摄像头")
            try:
                image_data_url = self.camera.capture_data_url()
            except Exception as exc:
                message = str(exc) if isinstance(exc, MacCameraError) else "摄像头读取失败"
                self._set_camera_state("error", message)
                raise
            self._set_camera_state("ready", "摄像头在线", captured=True)
            return image_data_url

    def analyze_pantry_image(self, image_data_url: str) -> dict[str, Any]:
        """Extract an editable pantry list from an uploaded or captured image."""
        if not self.client.is_available():
            raise RuntimeError("视觉识别尚未配置，请设置 DASHSCOPE_API_KEY。")
        _validate_image_data_url(image_data_url)
        payload = self.client.vision_json(image_data_url, _pantry_prompt())
        return _normalize_pantry_result(payload)

    def recognize(self, user_text: str) -> dict[str, Any]:
        if not self.client.is_available():
            return _error_result(
                "视觉识别还没有配置。请先设置 DASHSCOPE_API_KEY。",
                "视觉识别未配置",
            )

        started = time.perf_counter()
        try:
            image_data_url = self.capture_preview()
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

    def _set_camera_state(self, state: str, message: str, *, captured: bool = False) -> None:
        with self._state_lock:
            self._camera_state = state
            self._camera_message = message[:160]
            if captured:
                self._last_capture_at = time.time()


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


def _pantry_prompt() -> str:
    return (
        "你是厨房食材盘点助手。只识别图片中清晰可见、可用于做菜的食材，"
        "不要判断新鲜度、成熟度、过敏原或食品安全，也不要给事故处置建议。"
        "相同食材合并；包装遮挡或无法确认的物品放进uncertain_items，不要猜。"
        "只返回合法JSON，不要Markdown。结构："
        '{"ingredients":[{"name":string,"confidence":"高"|"中"}],'
        '"uncertain_items":[string],"summary":string,"needs_retake":boolean,'
        '"retake_instruction":string|null}。ingredients最多12项，name不含数量和形容词。'
    )


def _validate_image_data_url(value: str) -> None:
    if not isinstance(value, str) or not re.match(
        r"^data:image/(?:jpeg|jpg|png|webp);base64,[A-Za-z0-9+/=\r\n]+$",
        value,
        flags=re.IGNORECASE,
    ):
        raise ValueError("仅支持 JPEG、PNG 或 WebP 图片。")
    # Base64 expands binary data by roughly 4/3. Keep the decoded upload below 8 MiB.
    if len(value) > 11_200_000:
        raise ValueError("图片不能超过 8 MB。")


def _normalize_pantry_result(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("食材识别结果必须是对象。")
    rows = payload.get("ingredients")
    if not isinstance(rows, list):
        raise ValueError("食材识别结果缺少 ingredients。")
    ingredients: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in rows[:12]:
        if not isinstance(row, dict):
            continue
        name = re.sub(r"\s+", "", str(row.get("name") or "").strip())[:30]
        confidence = str(row.get("confidence") or "中").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        ingredients.append({
            "name": name,
            "confidence": confidence if confidence in {"高", "中"} else "中",
        })
    uncertain_items = _short_strings(payload.get("uncertain_items"), limit=6, max_length=40)
    summary = str(payload.get("summary") or "").strip()[:160]
    needs_retake = bool(payload.get("needs_retake"))
    instruction_raw = payload.get("retake_instruction")
    instruction = str(instruction_raw).strip()[:120] if instruction_raw else None
    if not ingredients and not uncertain_items:
        needs_retake = True
        instruction = instruction or "请靠近食材并保持画面清晰后重新拍摄。"
    return {
        "ingredients": ingredients,
        "uncertain_items": uncertain_items,
        "summary": summary or f"识别到 {len(ingredients)} 种可见食材。",
        "needs_retake": needs_retake,
        "retake_instruction": instruction,
    }


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
