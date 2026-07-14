from __future__ import annotations

from typing import Any, Protocol


class BaseLLMClient(Protocol):
    def structured_answer(self, user_text: str, context: dict[str, Any], output_schema: dict[str, Any]) -> dict[str, Any]: ...
