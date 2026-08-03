from __future__ import annotations

import base64
import sys
from typing import Any


class MacCameraError(RuntimeError):
    """Safe camera error that does not expose frame data."""


class MacCamera:
    """Capture one compressed JPEG from the default macOS camera."""

    def __init__(
        self,
        *,
        camera_index: int = 0,
        max_width: int = 1280,
        jpeg_quality: int = 78,
        warmup_frames: int = 6,
        cv2_module: Any | None = None,
    ) -> None:
        self.camera_index = camera_index
        self.max_width = max_width
        self.jpeg_quality = jpeg_quality
        self.warmup_frames = warmup_frames
        self._cv2_module = cv2_module

    def capture_data_url(self) -> str:
        cv2 = self._get_cv2()
        backend = getattr(cv2, "CAP_AVFOUNDATION", None) if sys.platform == "darwin" else None
        capture = (
            cv2.VideoCapture(self.camera_index, backend)
            if backend is not None
            else cv2.VideoCapture(self.camera_index)
        )
        try:
            if not capture.isOpened():
                raise MacCameraError(
                    "无法打开 Mac 摄像头。请检查相机权限，或关闭正在占用摄像头的应用。"
                )
            capture.set(getattr(cv2, "CAP_PROP_FRAME_WIDTH", 3), self.max_width)
            capture.set(getattr(cv2, "CAP_PROP_FRAME_HEIGHT", 4), 720)

            frame = None
            for _ in range(max(1, self.warmup_frames)):
                ok, candidate = capture.read()
                if ok and candidate is not None:
                    frame = candidate
            if frame is None:
                raise MacCameraError("摄像头已打开，但没有读取到有效画面。")

            height, width = frame.shape[:2]
            if width > self.max_width:
                target_height = max(1, round(height * self.max_width / width))
                frame = cv2.resize(
                    frame,
                    (self.max_width, target_height),
                    interpolation=getattr(cv2, "INTER_AREA", 3),
                )
            ok, encoded = cv2.imencode(
                ".jpg",
                frame,
                [getattr(cv2, "IMWRITE_JPEG_QUALITY", 1), self.jpeg_quality],
            )
            if not ok:
                raise MacCameraError("摄像头画面压缩失败。")
            payload = base64.b64encode(encoded.tobytes()).decode("ascii")
            return f"data:image/jpeg;base64,{payload}"
        finally:
            capture.release()

    def _get_cv2(self) -> Any:
        if self._cv2_module is not None:
            return self._cv2_module
        try:
            import cv2
        except ImportError as exc:
            raise MacCameraError(
                "缺少摄像头依赖 opencv-python，请先安装 requirements.txt。"
            ) from exc
        return cv2
