"""Single, secret-free configuration entry point for Qwen integration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


DEFAULT_BASE_URL = "https://ws-1jj0fvndfsqmsmid.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
DEFAULT_TEXT_MODEL = "qwen3-omni-flash"
DEFAULT_VISION_MODEL = "qwen3-vl-flash"
DEFAULT_TIMEOUT_SECONDS = 25.0
DEFAULT_VISION_TIMEOUT_SECONDS = 20.0
DEFAULT_MAX_RETRIES = 0
LOCAL_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "qwen.env"


def _settings() -> dict[str, str]:
    settings = dict(os.environ)
    # A local ignored file lets desktop launches use a corrected credential
    # without depending on the environment inherited when the app started.
    try:
        if LOCAL_CONFIG_PATH.stat().st_size > 16_384:
            return settings
        text = LOCAL_CONFIG_PATH.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        return settings
    allowed = {"DASHSCOPE_API_KEY", "QWEN_BASE_URL", "QWEN_TEXT_MODEL", "QWEN_VISION_MODEL", "QWEN_TIMEOUT_SECONDS", "QWEN_VISION_TIMEOUT_SECONDS", "QWEN_MAX_RETRIES"}
    for line in text.splitlines():
        key, separator, value = line.strip().removeprefix("export ").partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if separator and key.strip() in allowed and value:
            settings[key.strip()] = value
    return settings


def _read_positive_float(name: str, default: float, settings: dict[str, str] | None = None) -> float:
    raw = (settings if settings is not None else os.environ).get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _read_non_negative_int(name: str, default: int, settings: dict[str, str] | None = None) -> int:
    raw = (settings if settings is not None else os.environ).get(name)
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
        settings = _settings()
        return cls(
            api_key=settings.get("DASHSCOPE_API_KEY"),
            base_url=settings.get("QWEN_BASE_URL", DEFAULT_BASE_URL),
            text_model=settings.get("QWEN_TEXT_MODEL", DEFAULT_TEXT_MODEL),
            vision_model=settings.get("QWEN_VISION_MODEL", DEFAULT_VISION_MODEL),
            timeout=_read_positive_float("QWEN_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS, settings),
            vision_timeout=_read_positive_float(
                "QWEN_VISION_TIMEOUT_SECONDS",
                DEFAULT_VISION_TIMEOUT_SECONDS, settings,
            ),
            max_retries=_read_non_negative_int("QWEN_MAX_RETRIES", DEFAULT_MAX_RETRIES, settings),
        )
