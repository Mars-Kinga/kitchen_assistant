from __future__ import annotations

from typing import Any


def generation_error(exc: Exception | None, *, configured_ai: bool) -> dict[str, Any]:
    """Translate provider failures without exposing keys or response bodies."""
    statuses: set[int] = set()
    types: set[str] = set()
    seen: set[int] = set()
    cause: BaseException | None = exc
    while cause is not None and id(cause) not in seen:
        seen.add(id(cause))
        types.add(type(cause).__name__)
        status = getattr(cause, "status_code", None)
        if isinstance(status, int):
            statuses.add(status)
        cause = cause.__cause__ or cause.__context__

    code, message, retryable = "generation_failed", "菜谱生成服务没有返回可用菜谱，请稍后重试。", True
    if statuses & {401, 403} or types & {"AuthenticationError", "PermissionDeniedError"}:
        code = "authentication_failed"
        message = "菜谱生成接口鉴权失败，请检查接口地址、API 密钥及模型访问权限，更新配置并重启服务后重试。"
        retryable = False
    elif 404 in statuses or "NotFoundError" in types:
        code = "model_unavailable"
        message = "菜谱生成接口或模型不存在，请检查接口地址和模型名称，更新配置并重启服务后重试。"
        retryable = False
    elif 429 in statuses or "RateLimitError" in types:
        code, message = "rate_limited", "菜谱生成服务请求过于频繁或额度不足，请检查额度或稍后重试。"
    elif types & {"APITimeoutError", "TimeoutError", "ReadTimeout"}:
        code, message = "timeout", "菜谱生成服务响应超时，请稍后重试。"
    elif types & {"APIConnectionError", "ConnectError", "ConnectionError"}:
        code, message = "connection_failed", "无法连接菜谱生成服务，请检查网络及接口地址后重试。"
    elif types & {"ValueError", "QwenJSONOutputError", "JSONDecodeError"}:
        code, message = "invalid_recipe", "生成的菜谱未通过完整性检查，没有采用不完整的方案，请重新生成。"
    elif not configured_ai:
        code = "not_configured"
        message = "本地没有符合条件的菜谱，且未配置 AI 菜谱生成服务。请配置接口地址、API 密钥和模型，重启服务后重试。"
        retryable = False
    return {"code": code, "message": message, "retryable": retryable}
