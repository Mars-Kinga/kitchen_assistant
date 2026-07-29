from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any


def load_catalog(recipes_dir: Path) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """Load the base catalog plus sorted recipe files under ``catalog/``."""
    base_path = recipes_dir / "recipes.json"
    payload = json.loads(base_path.read_text(encoding="utf-8"))
    if isinstance(payload, list):  # Temporary read compatibility for old catalogs.
        recipes = _validate_recipe_list(payload, base_path)
        profiles: dict[str, Any] = {}
    elif isinstance(payload, dict):
        recipes = _read_recipe_list(payload, base_path)
        profiles = payload.get("dish_profiles", {})
    else:
        raise ValueError(f"菜谱目录根节点必须是对象或数组：{base_path}")
    if not isinstance(profiles, dict):
        raise ValueError("recipes.json 的 recipes 或 dish_profiles 格式无效")

    catalog_dir = recipes_dir / "catalog"
    if catalog_dir.is_dir():
        for path in sorted(catalog_dir.rglob("*.json")):
            recipes.extend(_read_recipe_list(_load_json_object(path), path))

    _validate_unique_recipe_identity(recipes, recipes_dir)
    return recipes, {str(name): profile for name, profile in profiles.items() if isinstance(profile, dict)}


def _load_json_object(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"菜谱目录根节点必须是对象：{path}")
    return payload


def _read_recipe_list(payload: dict[str, Any], path: Path) -> list[dict[str, Any]]:
    recipes = payload.get("recipes", [])
    if not isinstance(recipes, list):
        raise ValueError(f"菜谱目录的 recipes 必须是数组：{path}")
    return _validate_recipe_list(recipes, path)


def _validate_recipe_list(recipes: list[Any], path: Path) -> list[dict[str, Any]]:
    for index, recipe in enumerate(recipes):
        if not isinstance(recipe, dict):
            raise ValueError(f"菜谱目录的 recipes[{index}] 必须是对象：{path}")
    return [dict(recipe) for recipe in recipes]


def _validate_unique_recipe_identity(recipes: list[dict[str, Any]], recipes_dir: Path) -> None:
    seen_ids: dict[str, int] = {}
    seen_names: dict[str, int] = {}
    for index, recipe in enumerate(recipes):
        recipe_id = str(recipe.get("recipe_id", "")).strip()
        name = str(recipe.get("name", "")).strip()
        if not recipe_id:
            raise ValueError(f"菜谱目录第 {index + 1} 条缺少 recipe_id：{recipes_dir}")
        if not name:
            raise ValueError(f"菜谱目录第 {index + 1} 条缺少 name：{recipes_dir}")
        if recipe_id in seen_ids:
            raise ValueError(f"菜谱目录存在重复 recipe_id：{recipe_id}")
        if name in seen_names:
            raise ValueError(f"菜谱目录存在重复菜名：{name}")
        seen_ids[recipe_id] = index
        seen_names[name] = index


@lru_cache(maxsize=1)
def _default_profiles() -> dict[str, dict[str, Any]]:
    recipes_dir = Path(__file__).resolve().parents[1] / "recipes"
    return load_catalog(recipes_dir)[1]


def matching_profiles(*values: Any, profiles: dict[str, dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    source = profiles if profiles is not None else _default_profiles()
    text = " ".join(str(value or "") for value in values)
    matches: list[dict[str, Any]] = []
    for profile in source.values():
        terms = profile.get("match_terms", [])
        if isinstance(terms, list) and any(str(term) and str(term) in text for term in terms):
            matches.append(profile)
    return matches


def profile_prompt_rules(*values: Any, stage: str = "recipe") -> list[str]:
    rules: list[str] = []
    for profile in matching_profiles(*values):
        key = "candidate_prompt_rules" if stage == "candidate" else "recipe_prompt_rules"
        source = profile.get(key, profile.get("prompt_rules", []))
        for rule in source:
            if isinstance(rule, str) and rule.strip():
                rules.append(rule.strip())
    return rules


def profile_questions(dish: str | None) -> list[dict[str, Any]]:
    questions: list[dict[str, Any]] = []
    for profile in matching_profiles(dish):
        questions.extend(item for item in profile.get("preference_questions", []) if isinstance(item, dict))
    return questions
