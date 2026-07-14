from __future__ import annotations

from typing import Any


class MockLLMClient:
    def structured_answer(self, user_text: str, context: dict[str, Any], output_schema: dict[str, Any]) -> dict[str, Any]:
        return {"status": "offline", "answer": "当前没有配置在线模型，已使用本地规则。"}
