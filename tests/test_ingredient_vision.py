from __future__ import annotations

from types import SimpleNamespace

from runtime_core.ingredient_vision import IngredientVisionService, is_visual_identification_request
from runtime_core.mac_camera import MacCamera


class FakeEncoded:
    def tobytes(self) -> bytes:
        return b"jpeg-bytes"


class FakeCapture:
    def __init__(self) -> None:
        self.released = False
        self.frame = SimpleNamespace(shape=(720, 1280, 3))

    def isOpened(self) -> bool:
        return True

    def set(self, *_args) -> bool:
        return True

    def read(self):
        return True, self.frame

    def release(self) -> None:
        self.released = True


class FakeCV2:
    CAP_AVFOUNDATION = 1200
    CAP_PROP_FRAME_WIDTH = 3
    CAP_PROP_FRAME_HEIGHT = 4
    IMWRITE_JPEG_QUALITY = 1
    INTER_AREA = 3

    def __init__(self) -> None:
        self.capture = FakeCapture()
        self.video_capture_args = None

    def VideoCapture(self, *args):
        self.video_capture_args = args
        return self.capture

    @staticmethod
    def imencode(_extension, _frame, _options):
        return True, FakeEncoded()

    @staticmethod
    def resize(frame, size, *, interpolation):
        return SimpleNamespace(shape=(size[1], size[0], 3))


class FakeVisionClient:
    def __init__(self, *, available: bool = True) -> None:
        self.available = available
        self.image_data_url = None
        self.prompt = None

    def is_available(self) -> bool:
        return self.available

    def vision_json(self, image_data_url: str, prompt: str):
        self.image_data_url = image_data_url
        self.prompt = prompt
        return {
            "answer": "更像小葱",
            "candidates": ["小葱", "大葱"],
            "confidence_level": "中",
            "visual_evidence": ["叶片较细", "葱白较短"],
            "needs_retake": False,
            "retake_instruction": None,
        }


def test_visual_intent_requires_visible_food_reference() -> None:
    assert is_visual_identification_request("帮我看看这是大葱还是小葱")
    assert is_visual_identification_request("识别一下这个食材")
    assert is_visual_identification_request("这是什么菜")
    assert not is_visual_identification_request("我想看看番茄炒蛋怎么做")
    assert not is_visual_identification_request("你看看锅里熟了吗")


def test_mac_camera_captures_one_compressed_data_url() -> None:
    cv2 = FakeCV2()
    camera = MacCamera(cv2_module=cv2, warmup_frames=2)

    result = camera.capture_data_url()

    assert result.startswith("data:image/jpeg;base64,")
    assert cv2.capture.released is True


def test_vision_service_returns_safe_five_channel_result_without_image_data() -> None:
    cv2 = FakeCV2()
    client = FakeVisionClient()
    service = IngredientVisionService(
        MacCamera(cv2_module=cv2, warmup_frames=1),
        client,
    )

    result = service.recognize("这是大葱还是小葱")

    assert result["route"] == "vision_result"
    assert result["vision_result"]["answer"] == "更像小葱"
    assert "叶片较细" in result["speech"]
    assert client.image_data_url.startswith("data:image/jpeg;base64,")
    assert "data:image" not in str(result)
    assert result["latency_ms"]["total"] >= 0


def test_vision_service_fails_before_opening_camera_without_api_key() -> None:
    class CameraMustNotRun:
        def capture_data_url(self):
            raise AssertionError("camera should not be opened")

    result = IngredientVisionService(
        CameraMustNotRun(),
        FakeVisionClient(available=False),
    ).recognize("识别一下这个食材")

    assert result["display"] == "视觉识别未配置"
    assert "DASHSCOPE_API_KEY" in result["speech"]
