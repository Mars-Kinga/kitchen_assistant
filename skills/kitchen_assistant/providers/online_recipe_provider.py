from __future__ import annotations

import os
from typing import Any

from kitchen.models import RecipeCandidate, RecipeSearchRequest
from .mock_recipe_provider import MockRecipeSearchProvider


class OnlineRecipeSearchProvider:
    """Disabled adapter until the user supplies an approved recipe API contract.

    No HTTP request is attempted: this repository has no API documentation,
    endpoint, credentials, or permission to scrape third-party recipe sites.
    """

    def __init__(self, fallback: MockRecipeSearchProvider) -> None:
        self.fallback = fallback
        self.configured = bool(os.getenv("RECIPE_API_BASE_URL") and os.getenv("RECIPE_API_KEY"))
        self.mode = "web_search"

    def search_recipes(self, request: RecipeSearchRequest) -> list[RecipeCandidate]:
        raise RuntimeError("Web Search Provider 尚未配置官方搜索 API；请回退 Mock。")

    def get_recipe_detail(self, candidate: RecipeCandidate) -> dict[str, Any]:
        raise RuntimeError("Web Search Provider 尚未配置官方搜索 API。")
