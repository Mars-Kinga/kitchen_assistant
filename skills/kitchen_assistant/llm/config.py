"""Single, secret-free configuration entry point for Qwen integration."""

from __future__ import annotations

import os
from dataclasses import dataclass


DEFAULT_BASE_URL = "https://ws-1jj0fvndfsqmsmid.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
DEFAULT_TEXT_MODEL = "qwen3-omni-flash"
DEFAULT_VISION_MODEL = "qwen3-vl-flash"
DEFAULT_TIMEOUT_SECONDS = 25.0
DEFAULT_VISION_TIMEOUT_SECONDS = 20.0
DEFAULT_MAX_RETRIES = 0


def _read_positive_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _read_non_negative_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value >= 0 else default


@dataclass(frozen=True)
class QwenConfig:
    api_key: str | None
    base_url: str
    text_model: str
    vision_model: str
    timeout: float = DEFAULT_TIMEOUT_SECONDS
    vision_timeout: float = DEFAULT_VISION_TIMEOUT_SECONDS
    max_retries: int = DEFAULT_MAX_RETRIES

    @classmethod
    def from_environment(cls) -> "QwenConfig":
        return cls(
            api_key=os.getenv("DASHSCOPE_API_KEY"),
            base_url=os.getenv("QWEN_BASE_URL", DEFAULT_BASE_URL),
            text_model=os.getenv("QWEN_TEXT_MODEL", DEFAULT_TEXT_MODEL),
            vision_model=os.getenv("QWEN_VISION_MODEL", DEFAULT_VISION_MODEL),
            timeout=_read_positive_float("QWEN_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS),
            vision_timeout=_read_positive_float(
                "QWEN_VISION_TIMEOUT_SECONDS",
                DEFAULT_VISION_TIMEOUT_SECONDS,
            ),
            max_retries=_read_non_negative_int("QWEN_MAX_RETRIES", DEFAULT_MAX_RETRIES),
        )
