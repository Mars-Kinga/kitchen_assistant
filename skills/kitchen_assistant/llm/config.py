"""Single, secret-free configuration entry point for Doubao integration."""

from __future__ import annotations

import os
from dataclasses import dataclass


DEFAULT_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
DEFAULT_MODEL = "doubao-seed-2-0-mini-260428"


@dataclass(frozen=True)
class DoubaoConfig:
    api_key: str | None
    base_url: str
    model: str
    timeout: float = 30.0
    max_retries: int = 2

    @classmethod
    def from_environment(cls) -> "DoubaoConfig":
        return cls(
            api_key=os.getenv("ARK_API_KEY"),
            base_url=os.getenv("DOUBAO_BASE_URL", DEFAULT_BASE_URL),
            model=os.getenv("DOUBAO_MODEL", DEFAULT_MODEL),
        )
