from __future__ import annotations

from typing import Any

from .dietary_rules import ingredient_conflicts
from .ingredient_vocabulary import is_animal_protein


def recipe_respects_restrictions(recipe: dict[str, Any] | None, restrictions: list[str]) -> bool:
    if not recipe:
        return False
    names = [str(item.get("name", "")) for item in recipe.get("ingredients", [])]
    return not ingredient_conflicts(names, restrictions)


def animal_protein_names(recipe: dict[str, Any] | None) -> list[str]:
    if not recipe:
        return []
    found: list[str] = []
    for item in recipe.get("ingredients", []):
        name = str(item.get("name", ""))
        if is_animal_protein(name) and name not in found:
            found.append(name)
    return found
