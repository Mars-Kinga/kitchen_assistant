from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any


def load_catalog(recipes_dir: Path) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """Load canonical recipes and dish-specific policy data from one file."""
    payload = json.loads((recipes_dir / "recipes.json").read_text(encoding="utf-8"))
    if isinstance(payload, list):  # Temporary read compatibility for old catalogs.
        return payload, {}
    if not isinstance(payload, dict):
        raise ValueError("recipes.json 必须包含 recipes 和 dish_profiles")
    recipes = payload.get("recipes")
    profiles = payload.get("dish_profiles", {})
    if not isinstance(recipes, list) or not isinstance(profiles, dict):
        raise ValueError("recipes.json 的 recipes 或 dish_profiles 格式无效")
    return recipes, {str(name): profile for name, profile in profiles.items() if isinstance(profile, dict)}


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
