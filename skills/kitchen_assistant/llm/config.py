"""Single, secret-free configuration entry point for Doubao integration."""

from __future__ import annotations

import os
from dataclasses import dataclass


DEFAULT_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
DEFAULT_MODEL = "doubao-seed-2-0-mini-260428"
DEFAULT_TIMEOUT_SECONDS = 90.0
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
class DoubaoConfig:
    api_key: str | None
    base_url: str
    model: str
    timeout: float = DEFAULT_TIMEOUT_SECONDS
    max_retries: int = DEFAULT_MAX_RETRIES

    @classmethod
    def from_environment(cls) -> "DoubaoConfig":
        return cls(
            api_key=os.getenv("ARK_API_KEY"),
            base_url=os.getenv("DOUBAO_BASE_URL", DEFAULT_BASE_URL),
            model=os.getenv("DOUBAO_MODEL", DEFAULT_MODEL),
            timeout=_read_positive_float("DOUBAO_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS),
            max_retries=_read_non_negative_int("DOUBAO_MAX_RETRIES", DEFAULT_MAX_RETRIES),
        )
