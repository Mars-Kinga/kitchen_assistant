from __future__ import annotations

import json
import re
from typing import Any, Callable

from .config import DoubaoConfig


MAX_RESPONSE_CHARS = 30_000


class DoubaoClientError(RuntimeError):
    """A safe, key-free error suitable for provider fallback."""


class DoubaoLLMClient:
    """Chat Completions client for the confirmed Ark/Doubao API contract.

    The API key is read only from ``ARK_API_KEY``. ``client`` is injectable so
    tests can exercise every failure mode without a network request.
    """

    def __init__(
        self,
        *,
        client: Any | None = None,
        client_factory: Callable[..., Any] | None = None,
        config: DoubaoConfig | None = None,
        timeout: float | None = None,
        max_retries: int | None = None,
    ) -> None:
        configured = config or DoubaoConfig.from_environment()
        self.api_key = configured.api_key
        self.base_url = configured.base_url
        self.model = configured.model
        self.timeout = configured.timeout if timeout is None else timeout
        self.max_retries = configured.max_retries if max_retries is None else max_retries
        self._client = client
        self._client_factory = client_factory

    def is_available(self) -> bool:
        return bool(self.api_key)

    def chat(self, messages: list[dict[str, Any]]) -> str:
        if not self.is_available():
            raise DoubaoClientError("未设置 ARK_API_KEY，豆包功能未启用。")
        if not isinstance(messages, list) or not messages:
            raise DoubaoClientError("消息不能为空。")
        try:
            response = self._get_client().chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=0.2,
            )
            choices = getattr(response, "choices", None)
            if not choices:
                raise DoubaoClientError("豆包返回空 choices。")
            message = getattr(choices[0], "message", None)
            content = getattr(message, "content", None)
            if not isinstance(content, str) or not content.strip():
                raise DoubaoClientError("豆包返回空内容。")
            if len(content) > MAX_RESPONSE_CHARS:
                raise DoubaoClientError("豆包输出超过安全长度。")
            return content.strip()
        except DoubaoClientError:
            raise
        except Exception as exc:
            # Do not include provider exception text: it could contain request
            # details and is not useful to the end user.
            raise DoubaoClientError(f"豆包调用失败：{type(exc).__name__}") from exc

    def generate_json(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        content = self.chat(messages)
        try:
            return parse_json_response(content)
        except ValueError:
            repair_messages = [
                *messages,
                {
                    "role": "system",
                    "content": "上一条输出不是合法 JSON。请仅返回修复后的合法 JSON，不要 Markdown 或解释。",
                },
            ]
            try:
                return parse_json_response(self.chat(repair_messages))
            except (DoubaoClientError, ValueError) as second_error:
                raise DoubaoClientError("豆包 JSON 输出无效，已停止重试。") from second_error

    def structured_answer(self, user_text: str, context: dict[str, Any], output_schema: dict[str, Any]) -> dict[str, Any]:
        """Compatibility hook for the existing reserved LLM interface."""
        return self.generate_json([
            {"role": "system", "content": "只返回符合给定 JSON Schema 的 JSON。"},
            {"role": "user", "content": json.dumps({"text": user_text, "context": context, "schema": output_schema}, ensure_ascii=False)},
        ])

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        factory = self._client_factory
        if factory is None:
            try:
                from openai import OpenAI
            except ImportError as exc:
                raise DoubaoClientError("缺少 openai 依赖，请安装 requirements.txt。") from exc
            factory = OpenAI
        self._client = factory(
            base_url=self.base_url,
            api_key=self.api_key,
            timeout=self.timeout,
            max_retries=self.max_retries,
        )
        return self._client


def parse_json_response(content: str) -> dict[str, Any]:
    """Parse JSON safely, accepting a single accidental Markdown code block."""
    if not isinstance(content, str):
        raise ValueError("JSON 内容必须是字符串")
    text = content.strip()
    fence = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.IGNORECASE | re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    if not text or len(text) > MAX_RESPONSE_CHARS:
        raise ValueError("JSON 内容为空或过长")
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError("JSON 根节点必须是对象")
    return parsed
