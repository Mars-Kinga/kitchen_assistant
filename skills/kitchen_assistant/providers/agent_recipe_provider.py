from __future__ import annotations

from typing import Any

from kitchen.models import RecipeCandidate, RecipeSearchRequest
from .mock_recipe_provider import MockRecipeSearchProvider


class AgentRecipeProvider:
    """Future Agent/Doubao adapter placeholder; it intentionally stays offline."""

    def __init__(self, fallback: MockRecipeSearchProvider) -> None:
        self.fallback = fallback
        # This placeholder returns local data, so it must never be presented
        # as an Agent or web source.
        self.mode = "mock"

    def search_recipes(self, request: RecipeSearchRequest) -> list[RecipeCandidate]:
        return self.fallback.search_recipes(request)

    def get_recipe_detail(self, candidate: RecipeCandidate) -> dict[str, Any]:
        return self.fallback.get_recipe_detail(candidate)
