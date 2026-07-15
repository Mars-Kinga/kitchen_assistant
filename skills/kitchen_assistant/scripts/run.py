from __future__ import annotations

import sys
from pathlib import Path
from typing import Any


SKILL_ROOT = Path(__file__).resolve().parents[1]
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

from kitchen.cooking_question_service import DoubaoCookingQuestionService, RuleBasedCookingQuestionService  # noqa: E402
from kitchen.session_store import KitchenSession  # noqa: E402
from llm.doubao_client import DoubaoLLMClient  # noqa: E402
from providers.doubao_ai_recipe_provider import DoubaoAIRecipeProvider  # noqa: E402
from providers.mock_recipe_provider import MockRecipeSearchProvider  # noqa: E402


def _create_session() -> KitchenSession:
    """Select AI generation only when the user explicitly configured a key."""
    fallback = MockRecipeSearchProvider(
        SKILL_ROOT / "recipes",
        generated_dir=SKILL_ROOT / "recipes" / "generated",
    )
    llm_client = DoubaoLLMClient()
    if llm_client.is_available():
        provider = DoubaoAIRecipeProvider(llm_client, fallback)
        question_service = DoubaoCookingQuestionService(llm_client)
    else:
        provider = fallback
        question_service = RuleBasedCookingQuestionService()
    return KitchenSession(recipe_provider=provider, cooking_question_service=question_service)


# SkillManager caches this module, so this object lasts across CLI turns.
_SESSION = _create_session()


def run(arguments: dict[str, Any]) -> dict[str, Any]:
    return _SESSION.handle(str(arguments.get("user_text", "")))


def set_progress_callback(callback: Any | None) -> None:
    """Let the runtime render a waiting state before a blocking AI call."""
    _SESSION.set_progress_callback(callback)


def poll() -> dict[str, Any] | None:
    """Expose due kitchen timer events to the host runtime."""
    return _SESSION.poll()
