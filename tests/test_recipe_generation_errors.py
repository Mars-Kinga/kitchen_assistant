from __future__ import annotations

import sys
from pathlib import Path

import pytest

from runtime_core.kitchen_console import _recommendation_response


SKILL_ROOT = Path(__file__).resolve().parents[1] / "skills" / "kitchen_assistant"
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

from kitchen.recipe_errors import generation_error  # noqa: E402
from kitchen.session_store import KitchenSession  # noqa: E402
from providers.mock_recipe_provider import MockRecipeSearchProvider  # noqa: E402


@pytest.mark.parametrize("status,code", [
    (401, "authentication_failed"), (403, "authentication_failed"),
    (404, "model_unavailable"), (429, "rate_limited"),
])
def test_generation_error_classifies_nested_http_errors_without_secrets(status, code):
    cause = RuntimeError("secret-key and private provider response")
    cause.status_code = status
    wrapper = RuntimeError("private wrapper")
    wrapper.__cause__ = cause
    error = generation_error(wrapper, configured_ai=True)

    assert error["code"] == code
    assert "secret-key" not in str(error)
    assert "private" not in str(error)


@pytest.mark.parametrize("exception_type,code", [
    ("APITimeoutError", "timeout"), ("APIConnectionError", "connection_failed"),
    ("QwenJSONOutputError", "invalid_recipe"), ("ValueError", "invalid_recipe"),
])
def test_generation_error_classifies_retryable_errors(exception_type, code):
    error = generation_error(type(exception_type, (Exception,), {})("private"), configured_ai=True)
    assert error["code"] == code
    assert error["retryable"] is True


def test_photo_inventory_uses_partial_local_match_without_generation_config():
    session = KitchenSession(recipe_provider=MockRecipeSearchProvider(SKILL_ROOT / "recipes"))
    result = session.recommend_from_ingredients(
        ["牛肋排", "玉米", "小番茄", "西兰花", "生菜", "青椒", "牛奶", "蜂蜜"],
        servings=1, taste="正常",
    )
    assert result["recipe_candidates"]
    assert all(candidate["unused_ingredients"] for candidate in result["recipe_candidates"])
    assert "recommendation_error" not in result
    assert _recommendation_response(result)["recommendation_status"] == "ready"


def test_failed_ai_search_reports_authentication_in_console_and_robot_feedback(capsys):
    class BrokenProvider:
        supports_ai = True
        mode = "ai_generated"

        def search_recipes(self, request):
            cause = RuntimeError("provider response must remain private")
            cause.status_code = 401
            raise RuntimeError("generation failed") from cause

    session = KitchenSession(recipe_provider=BrokenProvider())
    result = session.recommend_from_ingredients(["玉米", "牛奶"], servings=1, taste="正常")
    response = _recommendation_response(result)

    assert response["recommendation_status"] == "failed"
    assert response["recommendation_error"]["code"] == "authentication_failed"
    assert "鉴权失败" in response["message"]
    assert "鉴权失败" in result["speech"]
    assert "正在" not in response["message"]
    assert "private" not in str(response)
    assert "private" not in capsys.readouterr().out


def test_successful_response_uses_terminal_feedback_not_stale_searching_message():
    result = {
        "recipe_candidates": [{"title": "牛奶玉米"}], "provider_mode": "ai_generated",
        "steps": [{"display": "正在搜索"}, {"display": "已生成菜谱"}],
    }
    response = _recommendation_response(result)
    assert response["recommendation_status"] == "ready"
    assert response["message"] == "已生成菜谱"
    assert response["recommendation_error"] is None
