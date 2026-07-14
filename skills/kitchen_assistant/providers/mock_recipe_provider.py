from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from kitchen.cache import MemoryRecipeCache
from kitchen.models import RecipeCandidate, RecipeSearchRequest
from kitchen.recommendation_service import rank_recipes


class MockRecipeSearchProvider:
    """Offline demonstration provider; it never claims to search the web."""

    mode = "mock"

    def __init__(
        self,
        recipes_dir: Path,
        cache: MemoryRecipeCache | None = None,
        generated_dir: Path | None = None,
    ) -> None:
        self.recipes_dir = recipes_dir
        self.cache = cache or MemoryRecipeCache()
        self.generated_dir = generated_dir
        self._generated_recipe_ids: set[str] = set()
        catalog = json.loads((recipes_dir / "recipes.json").read_text(encoding="utf-8"))
        if not isinstance(catalog, list):
            raise ValueError("recipes.json 必须是菜谱数组")
        self._recipes: dict[str, dict[str, Any]] = {}
        for recipe in catalog:
            if not isinstance(recipe, dict) or not recipe.get("recipe_id") or not recipe.get("name"):
                continue
            recipe.setdefault("source_name", "Mock Recipe Provider")
            recipe.setdefault("source_url", None)
            self._recipes[str(recipe["recipe_id"])] = recipe
        if "tomato_egg" not in self._recipes:
            raise ValueError("recipes.json 缺少 tomato_egg 回退菜谱")
        self._load_generated_recipes()

    def search_recipes(self, request: RecipeSearchRequest) -> list[RecipeCandidate]:
        key = ("search", request.as_cache_key())
        cached = self.cache.get(key)
        if cached is not None:
            return cached
        recipes = list(self._recipes.values())
        if request.requested_dish:
            recipes = [recipe for recipe in recipes if _matches_requested_dish(recipe, request.requested_dish)]
        recipes = [recipe for recipe in recipes if self._cache_request_matches(recipe, request)]
        results = rank_recipes(recipes, request)
        self.cache.set(key, results)
        return results

    def search_cached_recipes(self, request: RecipeSearchRequest) -> list[RecipeCandidate]:
        """Return only previously persisted AI recipes compatible with this request."""
        if not request.requested_dish:
            return []
        recipes = [
            self._recipes[recipe_id]
            for recipe_id in self._generated_recipe_ids
            if recipe_id in self._recipes
            and _matches_requested_dish(self._recipes[recipe_id], request.requested_dish)
            and self._cache_request_matches(self._recipes[recipe_id], request)
        ]
        return rank_recipes(recipes, request)

    def save_generated_recipe(self, recipe: dict[str, Any], request: RecipeSearchRequest) -> Path | None:
        """Persist a validated generated recipe as a code-local JSON cache."""
        if self.generated_dir is None:
            return None
        stored = deepcopy(recipe)
        name = str(stored.get("name", "")).strip()
        if not name or not stored.get("ingredients") or not stored.get("steps"):
            raise ValueError("不能缓存缺少菜名、食材或步骤的菜谱")
        cache_request = {
            "requested_dish": request.requested_dish or name,
            "servings": request.servings,
            "taste_preferences": list(request.taste_preferences),
            "dietary_restrictions": list(request.dietary_restrictions),
        }
        # Include the candidate identity so three variants with the same
        # display name still get independent cache files.
        identity = json.dumps([stored.get("recipe_id") or name, name, cache_request], ensure_ascii=False, sort_keys=True)
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
        recipe_id = f"cached_{digest}"
        stored.update({
            "recipe_id": recipe_id,
            "source_name": "本地缓存菜谱",
            "source_url": None,
            "_cache_request": cache_request,
        })
        self.generated_dir.mkdir(parents=True, exist_ok=True)
        destination = self.generated_dir / f"{recipe_id}.json"
        temporary = destination.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(stored, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(destination)
        self._recipes[recipe_id] = stored
        self._generated_recipe_ids.add(recipe_id)
        self.cache = MemoryRecipeCache()
        return destination

    def get_recipe_detail(self, candidate: RecipeCandidate) -> dict[str, Any]:
        detail = self._recipes.get(candidate.candidate_id)
        if detail is None:
            raise KeyError(f"离线示例菜谱不存在：{candidate.candidate_id}")
        return dict(detail)

    def fallback_recipe(self) -> dict[str, Any]:
        return dict(self._recipes["tomato_egg"])

    def _load_generated_recipes(self) -> None:
        if self.generated_dir is None or not self.generated_dir.exists():
            return
        for path in sorted(self.generated_dir.glob("*.json")):
            try:
                recipe = json.loads(path.read_text(encoding="utf-8"))
                recipe_id = str(recipe["recipe_id"])
                if not recipe.get("name") or not recipe.get("ingredients") or not recipe.get("steps"):
                    continue
                recipe["source_name"] = "本地缓存菜谱"
                recipe["source_url"] = None
                self._recipes[recipe_id] = recipe
                self._generated_recipe_ids.add(recipe_id)
            except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
                continue

    def _cache_request_matches(self, recipe: dict[str, Any], request: RecipeSearchRequest) -> bool:
        recipe_id = str(recipe.get("recipe_id", ""))
        if recipe_id not in self._generated_recipe_ids:
            return True
        cached = recipe.get("_cache_request")
        if not isinstance(cached, dict):
            return True
        cached_servings = cached.get("servings")
        if cached_servings and request.servings and cached_servings != request.servings:
            return False
        return (
            sorted(cached.get("taste_preferences") or []) == sorted(request.taste_preferences)
            and sorted(cached.get("dietary_restrictions") or []) == sorted(request.dietary_restrictions)
        )


def _matches_requested_dish(recipe: dict[str, Any], requested_dish: str) -> bool:
    """Never substitute a different local dish for the one the user named."""
    title = str(recipe.get("name", "")).replace(" ", "")
    requested = str(requested_dish).replace(" ", "")
    return bool(title and requested and (requested in title or title in requested))
