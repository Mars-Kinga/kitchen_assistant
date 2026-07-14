from __future__ import annotations

from typing import Any, Protocol

from kitchen.models import RecipeCandidate, RecipeSearchRequest


class RecipeSearchProvider(Protocol):
    mode: str

    def search_recipes(self, request: RecipeSearchRequest) -> list[RecipeCandidate]: ...

    def get_recipe_detail(self, candidate: RecipeCandidate) -> dict[str, Any]: ...
