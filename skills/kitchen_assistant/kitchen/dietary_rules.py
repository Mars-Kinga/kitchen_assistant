from __future__ import annotations

from typing import Iterable

from .ingredient_vocabulary import restriction_matches


def ingredient_conflicts(ingredients: Iterable[str], restrictions: Iterable[str]) -> list[str]:
    """Return user restrictions that conflict with a recipe ingredient.

    Matching is deliberately small and deterministic.  Substring matching
    covers common pairs such as ``辣``/``辣椒`` and ``牛肉``/``肥牛`` without
    asking an LLM to make a safety decision.
    """
    names = [str(name).strip() for name in ingredients if str(name).strip()]
    conflicts: list[str] = []
    for raw in restrictions:
        restriction = str(raw).strip()
        if not restriction:
            continue
        if any(_matches(restriction, name) for name in names) and restriction not in conflicts:
            conflicts.append(restriction)
    return conflicts


def _matches(restriction: str, ingredient: str) -> bool:
    return restriction_matches(restriction, ingredient)
