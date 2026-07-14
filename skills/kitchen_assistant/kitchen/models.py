from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class RecipeSearchRequest:
    requested_dish: str | None = None
    available_ingredients: list[str] = field(default_factory=list)
    servings: int | None = None
    taste_preferences: list[str] = field(default_factory=list)
    dietary_restrictions: list[str] = field(default_factory=list)
    max_cooking_minutes: int | None = None
    available_equipment: list[str] = field(default_factory=list)
    difficulty_preference: str | None = None
    steak_doneness: str | None = None
    steak_thickness_cm: float | None = None
    excluded_candidate_ids: list[str] = field(default_factory=list)

    def as_cache_key(self) -> tuple[object, ...]:
        return (
            self.requested_dish, tuple(sorted(self.available_ingredients)), self.servings,
            tuple(sorted(self.taste_preferences)), tuple(sorted(self.dietary_restrictions)),
            self.max_cooking_minutes, tuple(sorted(self.available_equipment)),
            self.difficulty_preference, self.steak_doneness, self.steak_thickness_cm,
            tuple(sorted(self.excluded_candidate_ids)),
        )


@dataclass
class RecipeCandidate:
    candidate_id: str
    title: str
    source_name: str
    source_url: str | None
    summary: str
    estimated_minutes: int | None
    difficulty: str
    main_ingredients: list[str]
    missing_ingredients: list[str]
    match_reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "title": self.title,
            "source_name": self.source_name,
            "source_url": self.source_url,
            "summary": self.summary,
            "estimated_minutes": self.estimated_minutes,
            "difficulty": self.difficulty,
            "main_ingredients": self.main_ingredients,
            "missing_ingredients": self.missing_ingredients,
            "match_reason": self.match_reason,
        }


@dataclass
class CookingContext:
    recipe: dict[str, Any]
    current_step: dict[str, Any]
    servings: int | None
    taste_preferences: list[str]
    dietary_restrictions: list[str]
    available_ingredients: list[str]
    available_equipment: list[str]
    timer_remaining_seconds: int | None
    conversation_summary: list[str]


@dataclass
class CookingAnswer:
    answer: str
    display_text: str
    safety_level: str = "NORMAL"
    should_pause_cooking: bool = False
    robot_action: str = "nod"
    led_effect: str = "blue"
    expression: str = "focused"
