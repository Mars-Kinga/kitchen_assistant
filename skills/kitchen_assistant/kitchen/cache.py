from __future__ import annotations

from typing import Any


class MemoryRecipeCache:
    """Small, optional process-local cache; it never stores credentials."""

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled
        self._values: dict[object, Any] = {}

    def get(self, key: object) -> Any | None:
        return self._values.get(key) if self.enabled else None

    def set(self, key: object, value: Any) -> None:
        if self.enabled and value:
            self._values[key] = value
