from __future__ import annotations

import sys
from pathlib import Path
from typing import Any


SKILL_ROOT = Path(__file__).resolve().parents[1]
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

from kitchen.cooking_question_service import QwenCookingQuestionService, RuleBasedCookingQuestionService  # noqa: E402
from kitchen.session_store import KitchenSession  # noqa: E402
from llm.qwen_client import QwenLLMClient  # noqa: E402
from providers.mock_recipe_provider import MockRecipeSearchProvider  # noqa: E402
from providers.qwen_ai_recipe_provider import QwenAIRecipeProvider  # noqa: E402


def _create_session() -> KitchenSession:
    """Select AI generation only when the user explicitly configured a key."""
    fallback = MockRecipeSearchProvider(
        SKILL_ROOT / "recipes",
        generated_dir=SKILL_ROOT / "recipes" / "generated",
    )
    llm_client = QwenLLMClient()
    if llm_client.is_available():
        provider = QwenAIRecipeProvider(llm_client, fallback)
        question_service = QwenCookingQuestionService(llm_client)
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


def status_snapshot() -> dict[str, Any]:
    """Expose a read-only snapshot to the local kitchen console."""
    return _SESSION.status_snapshot()


def recommend_from_ingredients(
    ingredients: list[str],
    *,
    servings: int = 1,
    taste: str = "正常",
) -> dict[str, Any]:
    """Use a user-confirmed photo inventory to begin recipe selection."""
    return _SESSION.recommend_from_ingredients(
        ingredients,
        servings=servings,
        taste=taste,
    )


def browse_local_recipes(query: str = "", *, category: str = "", limit: int = 500) -> list[dict[str, Any]]:
    return _SESSION.browse_local_recipes(query, category=category, limit=limit)


def browse_recipe_categories() -> list[dict[str, Any]]:
    return _SESSION.browse_recipe_categories()


def generate_console_recipe(dish_name: str, *, servings: int = 2) -> dict[str, Any]:
    return _SESSION.generate_console_recipe(dish_name, servings=servings)


def console_recipe_detail(recipe_id: str, *, servings: int = 2) -> dict[str, Any]:
    return _SESSION.console_recipe_detail(recipe_id, servings=servings)


def select_console_recipe(recipe_id: str, *, servings: int = 2) -> dict[str, Any]:
    return _SESSION.select_console_recipe(recipe_id, servings=servings)


def select_imported_console_recipe(recipe: dict[str, Any]) -> dict[str, Any]:
    return _SESSION.select_imported_console_recipe(recipe)


def navigate_console_step(direction: str) -> dict[str, Any]:
    return _SESSION.navigate_console_step(direction)


def console_cooking_action(action: str) -> dict[str, Any]:
    return _SESSION.console_cooking_action(action)
