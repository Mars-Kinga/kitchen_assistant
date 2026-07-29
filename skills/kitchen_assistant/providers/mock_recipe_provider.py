from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any

from kitchen.cache import MemoryRecipeCache
from kitchen.dish_profiles import load_catalog
from kitchen.models import RecipeCandidate, RecipeSearchRequest
from kitchen.recipe_normalizer import RecipeNormalizationError, RecipeNormalizer
from kitchen.recommendation_service import rank_recipes


class MockRecipeSearchProvider:
    """Offline demonstration provider; it never claims to search the web."""

    mode = "mock"
    CACHE_VERSION = 5

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
        catalog, self.dish_profiles = load_catalog(recipes_dir)
        self._recipes: dict[str, dict[str, Any]] = {}
        for recipe in catalog:
            if not isinstance(recipe, dict) or not recipe.get("recipe_id") or not recipe.get("name"):
                continue
            recipe.setdefault("source_name", "Mock Recipe Provider")
            recipe.setdefault("source_url", None)
            self._recipes[str(recipe["recipe_id"])] = recipe
        if "tomato_egg" not in self._recipes:
            raise ValueError("recipes.json 缺少 tomato_egg 回退菜谱")
        self._cleanup_legacy_cache_files()
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
        """Return persisted AI recipes compatible with a dish or pantry request."""
        recipes = [
            self._recipes[recipe_id]
            for recipe_id in self._generated_recipe_ids
            if recipe_id in self._recipes
            # A named dish must retain its title.  For an ingredient-led
            # request, rank_recipes below requires every stated ingredient,
            # so a previously generated “蘑菇 + 牛肉” recipe can be reused
            # without another model call.
            and (
                not request.requested_dish
                or _matches_requested_dish(self._recipes[recipe_id], request.requested_dish)
            )
            and self._cache_request_matches(self._recipes[recipe_id], request)
        ]
        return rank_recipes(recipes, request)

    def save_generated_recipe(self, recipe: dict[str, Any], request: RecipeSearchRequest) -> Path | None:
        """Persist generated variants in one readable, dish-level JSON bundle."""
        if self.generated_dir is None:
            return None
        stored = deepcopy(recipe)
        name = str(stored.get("name", "")).strip()
        if not name or not stored.get("ingredients") or not stored.get("steps"):
            raise ValueError("不能缓存缺少菜名、食材或步骤的菜谱")
        cache_request = {
            "cache_version": self.CACHE_VERSION,
            "requested_dish": request.requested_dish or name,
            "servings": request.servings,
            "taste_preferences": list(request.taste_preferences),
            "dietary_restrictions": list(request.dietary_restrictions),
        }
        # A stored recipe is a base recipe.  The normalizer scales it when a
        # user chooses another serving count, so servings must not create a
        # second generated copy or trigger another model request.
        cache_identity = {
            "cache_version": self.CACHE_VERSION,
            "requested_dish": cache_request["requested_dish"],
            "taste_preferences": cache_request["taste_preferences"],
            "dietary_restrictions": cache_request["dietary_restrictions"],
        }
        identity = str(stored.get("recipe_id") or name)
        digest = hashlib.sha256(json.dumps([identity, cache_identity], ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:12]
        recipe_id = f"cached_{_safe_slug(name)}_{digest}"
        stored.update({
            "recipe_id": recipe_id,
            "source_name": "已保存菜谱",
            "source_url": None,
            "_cache_request": cache_request,
        })
        self.generated_dir.mkdir(parents=True, exist_ok=True)
        destination = self.generated_dir / f"cached_{_safe_slug(cache_request['requested_dish'])}.json"
        bundle: dict[str, Any] = {"cache_version": self.CACHE_VERSION, "recipes": []}
        if destination.exists():
            try:
                existing = json.loads(destination.read_text(encoding="utf-8"))
                if isinstance(existing, dict) and isinstance(existing.get("recipes"), list):
                    bundle["recipes"] = [item for item in existing["recipes"] if isinstance(item, dict)]
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                pass
        bundle["recipes"] = [item for item in bundle["recipes"] if str(item.get("recipe_id", "")) != recipe_id]
        bundle["recipes"].append(stored)
        temporary = destination.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(bundle, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(destination)
        self._recipes[recipe_id] = stored
        self._generated_recipe_ids.add(recipe_id)
        self.cache = MemoryRecipeCache()
        return destination

    def get_recipe_detail(self, candidate: RecipeCandidate) -> dict[str, Any]:
        detail = self._recipes.get(candidate.candidate_id)
        if detail is None:
            raise KeyError(f"本地菜谱不存在：{candidate.candidate_id}")
        return dict(detail)

    def fallback_recipe(self) -> dict[str, Any]:
        return dict(self._recipes["tomato_egg"])

    def _load_generated_recipes(self) -> None:
        if self.generated_dir is None or not self.generated_dir.exists():
            return
        for path in sorted(self.generated_dir.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                recipes = payload.get("recipes") if isinstance(payload, dict) else None
                if not isinstance(recipes, list):
                    recipes = [payload]
                for recipe in recipes:
                    if not isinstance(recipe, dict) or not recipe.get("recipe_id"):
                        continue
                    recipe_id = str(recipe["recipe_id"])
                    if not recipe.get("name") or not recipe.get("ingredients") or not recipe.get("steps"):
                        continue
                    try:
                        RecipeNormalizer().normalize(recipe)
                    except RecipeNormalizationError:
                        continue
                    recipe["source_name"] = "已保存菜谱"
                    recipe["source_url"] = None
                    self._recipes[recipe_id] = recipe
                    self._generated_recipe_ids.add(recipe_id)
            except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
                continue

    def _cleanup_legacy_cache_files(self) -> None:
        """Remove outdated generated caches whose safety rules may differ."""
        if self.generated_dir is None or not self.generated_dir.exists():
            return
        for path in self.generated_dir.glob("cached_*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict):
                continue
            if isinstance(payload.get("recipes"), list):
                if payload.get("cache_version") != self.CACHE_VERSION:
                    try:
                        path.unlink()
                    except OSError:
                        continue
                continue
            if not payload.get("recipe_id") or not payload.get("name"):
                continue
            cache_request = payload.get("_cache_request")
            if not isinstance(cache_request, dict) or cache_request.get("cache_version") != self.CACHE_VERSION:
                try:
                    path.unlink()
                except OSError:
                    continue

    def _cache_request_matches(self, recipe: dict[str, Any], request: RecipeSearchRequest) -> bool:
        recipe_id = str(recipe.get("recipe_id", ""))
        if recipe_id not in self._generated_recipe_ids:
            return True
        cached = recipe.get("_cache_request")
        if not isinstance(cached, dict):
            return False
        if cached.get("cache_version") != self.CACHE_VERSION:
            return False
        # The saved recipe's servings are its source baseline.  Details are
        # scaled by RecipeNormalizer for the current request, so a 1-person
        # cache is also valid for 2 or 3 people without consuming tokens.
        return (
            sorted(cached.get("taste_preferences") or []) == sorted(request.taste_preferences)
            and sorted(cached.get("dietary_restrictions") or []) == sorted(request.dietary_restrictions)
        )


def _matches_requested_dish(recipe: dict[str, Any], requested_dish: str) -> bool:
    """Never substitute a different local dish for the one the user named."""
    title = str(recipe.get("name", "")).replace(" ", "")
    requested = str(requested_dish).replace(" ", "")
    return bool(title and requested and (requested in title or title in requested))


def _safe_slug(value: str) -> str:
    """Keep cache filenames readable while supporting Chinese dish names."""
    text = str(value or "菜谱").strip().lower().replace(" ", "_")
    text = re.sub(r"[^0-9a-zA-Z_\u3400-\u9fff-]+", "_", text)
    text = re.sub(r"_+", "_", text).strip("_.-")
    return text[:48] or "recipe"
