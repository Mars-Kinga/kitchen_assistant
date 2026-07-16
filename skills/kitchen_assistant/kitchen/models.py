from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


_SEASONING_MARKERS = (
    "盐", "糖", "生抽", "老抽", "酱油", "醋", "料酒", "蚝油", "豆瓣",
    "胡椒", "花椒", "辣椒", "鸡精", "味精", "五香粉", "食用油", "橄榄油",
    "黄油", "芝麻油", "香油", "淀粉",
)


def split_main_foods_and_seasonings(labels: list[str]) -> tuple[list[str], list[str]]:
    """Keep candidate food names and key seasonings readable as separate lists."""
    foods: list[str] = []
    seasonings: list[str] = []
    for label in labels:
        value = str(label).strip()
        if not value:
            continue
        target = seasonings if any(marker in value for marker in _SEASONING_MARKERS) else foods
        if value not in target:
            target.append(value)
    return foods or seasonings, seasonings


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
    unavailable_equipment: list[str] = field(default_factory=list)
    equipment_only: bool = False
    bypass_cache: bool = False

    def as_cache_key(self) -> tuple[object, ...]:
        return (
            self.requested_dish, tuple(sorted(self.available_ingredients)), self.servings,
            tuple(sorted(self.taste_preferences)), tuple(sorted(self.dietary_restrictions)),
            self.max_cooking_minutes, tuple(sorted(self.available_equipment)),
            self.difficulty_preference, self.steak_doneness, self.steak_thickness_cm,
            tuple(sorted(self.excluded_candidate_ids)), tuple(sorted(self.unavailable_equipment)),
            self.equipment_only, self.bypass_cache,
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
    main_seasonings: list[str] = field(default_factory=list)

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
            "main_seasonings": self.main_seasonings,
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
