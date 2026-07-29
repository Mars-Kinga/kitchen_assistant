from __future__ import annotations

import json
import re
from typing import Any, Callable

from .config import QwenConfig


MAX_RESPONSE_CHARS = 30_000
VISION_MAX_TOKENS = 384


class QwenClientError(RuntimeError):
    """A safe, key-free error suitable for provider fallback."""


class QwenJSONOutputError(QwenClientError):
    """JSON parse failure carrying model text for one provider-level correction."""

    def __init__(self, raw_content: str) -> None:
        super().__init__("千问 JSON 输出无效。")
        self.raw_content = raw_content


class QwenLLMClient:
    """Low-latency Qwen client for text and vision Chat Completions.

    Text generation uses Qwen Omni, which requires streaming. The stream is
    collected internally so callers keep receiving a normal string. Vision
    uses the Qwen VL model with a short non-streaming structured response.
    Thinking is explicitly disabled for both paths.
    """

    def __init__(
        self,
        *,
        client: Any | None = None,
        client_factory: Callable[..., Any] | None = None,
        config: QwenConfig | None = None,
        timeout: float | None = None,
        vision_timeout: float | None = None,
        max_retries: int | None = None,
    ) -> None:
        configured = config or QwenConfig.from_environment()
        self.api_key = configured.api_key
        self.base_url = configured.base_url
        self.text_model = configured.text_model
        self.vision_model = configured.vision_model
        # Compatibility for provider diagnostics that previously read .model.
        self.model = self.text_model
        self.timeout = configured.timeout if timeout is None else timeout
        self.vision_timeout = configured.vision_timeout if vision_timeout is None else vision_timeout
        self.max_retries = configured.max_retries if max_retries is None else max_retries
        self._client = client
        self._client_factory = client_factory

    def is_available(self) -> bool:
        return bool(self.api_key)

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int | None = None,
        json_mode: bool = False,
        timeout: float | None = None,
    ) -> str:
        if not self.is_available():
            raise QwenClientError("未设置 DASHSCOPE_API_KEY，千问功能未启用。")
        if not isinstance(messages, list) or not messages:
            raise QwenClientError("消息不能为空。")
        try:
            request_timeout = self.timeout if timeout is None else max(float(timeout), 0.1)
            request: dict[str, Any] = {
                "model": self.text_model,
                "messages": messages,
                "stream": True,
                "modalities": ["text"],
                "temperature": 0.1,
                "timeout": request_timeout,
                "extra_body": {"enable_thinking": False},
            }
            if max_tokens is not None:
                request["max_completion_tokens"] = max_tokens
            if json_mode:
                # Qwen JSON Mode works with streaming and non-thinking mode.
                # This prevents prose/code fences and materially reduces
                # post-generation JSON parse failures.
                request["response_format"] = {"type": "json_object"}
            stream = self._get_client().chat.completions.create(
                **request,
            )
            content = _collect_stream_text(stream)
            if not content:
                raise QwenClientError("千问返回空内容。")
            if len(content) > MAX_RESPONSE_CHARS:
                raise QwenClientError("千问输出超过安全长度。")
            return content
        except QwenClientError:
            raise
        except Exception as exc:
            raise QwenClientError(f"千问调用失败：{type(exc).__name__}") from exc

    def generate_json(
        self,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        content = self.chat(
            messages,
            max_tokens=max_tokens,
            json_mode=True,
            timeout=timeout,
        )
        try:
            return parse_json_response(content)
        except ValueError as exc:
            # The provider may make one bounded correction request. Keep the
            # model text off the exception message and runtime logs.
            raise QwenJSONOutputError(content) from exc

    def vision_json(self, image_data_url: str, prompt: str) -> dict[str, Any]:
        if not self.is_available():
            raise QwenClientError("未设置 DASHSCOPE_API_KEY，视觉识别未启用。")
        if not image_data_url.startswith("data:image/"):
            raise QwenClientError("视觉输入必须是图片 Data URL。")
        messages = [{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": image_data_url}},
                {"type": "text", "text": prompt},
            ],
        }]
        try:
            response = self._get_client().chat.completions.create(
                model=self.vision_model,
                messages=messages,
                stream=False,
                temperature=0,
                max_tokens=VISION_MAX_TOKENS,
                timeout=self.vision_timeout,
                extra_body={"enable_thinking": False},
            )
            choices = getattr(response, "choices", None)
            if not choices:
                raise QwenClientError("千问视觉模型返回空 choices。")
            message = getattr(choices[0], "message", None)
            content = getattr(message, "content", None)
            if not isinstance(content, str) or not content.strip():
                raise QwenClientError("千问视觉模型返回空内容。")
            return parse_json_response(content)
        except QwenClientError:
            raise
        except ValueError as exc:
            raise QwenClientError("千问视觉 JSON 输出无效。") from exc
        except Exception as exc:
            raise QwenClientError(f"千问视觉调用失败：{type(exc).__name__}") from exc

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
                raise QwenClientError("缺少 openai 依赖，请安装 requirements.txt。") from exc
            factory = OpenAI
        self._client = factory(
            base_url=self.base_url,
            api_key=self.api_key,
            timeout=self.timeout,
            max_retries=self.max_retries,
        )
        return self._client


def _collect_stream_text(stream: Any) -> str:
    parts: list[str] = []
    total_length = 0
    saw_choice = False
    for chunk in stream:
        choices = getattr(chunk, "choices", None)
        if not choices:
            continue
        saw_choice = True
        delta = getattr(choices[0], "delta", None)
        content = getattr(delta, "content", None)
        if isinstance(content, str) and content:
            parts.append(content)
            total_length += len(content)
            if total_length > MAX_RESPONSE_CHARS:
                raise QwenClientError("千问输出超过安全长度。")
    if not saw_choice:
        raise QwenClientError("千问返回空 choices。")
    return "".join(parts).strip()


def parse_json_response(content: str) -> dict[str, Any]:
    """Parse JSON safely, tolerating a small accidental wrapper.

    JSON Mode should return a bare object, but compatible gateways can still
    occasionally add a short preface or suffix. Accept one complete object
    without attempting to repair genuinely truncated JSON.
    """
    if not isinstance(content, str):
        raise ValueError("JSON 内容必须是字符串")
    text = content.strip()
    fence = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.IGNORECASE | re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    if not text or len(text) > MAX_RESPONSE_CHARS:
        raise ValueError("JSON 内容为空或过长")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as original_error:
        start = text.find("{")
        if start < 0:
            raise
        try:
            parsed, end = json.JSONDecoder().raw_decode(text[start:])
        except json.JSONDecodeError:
            raise original_error
        trailing = text[start + end:].strip()
        if trailing.startswith(("```", "。", "，", ",", "谢谢", "以上")):
            trailing = trailing.lstrip("`。，, \r\n")
            if trailing and not trailing.startswith(("谢谢", "以上")):
                raise original_error
        elif trailing:
            raise original_error
    if not isinstance(parsed, dict):
        raise ValueError("JSON 根节点必须是对象")
    return parsed
