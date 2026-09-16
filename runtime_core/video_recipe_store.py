"""Persistent storage for recipes imported from cooking videos."""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
import threading
import uuid
from copy import deepcopy
from fractions import Fraction
from pathlib import Path
from typing import Any


DEFAULT_IMPORTED_RECIPE_DIR = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "kitchen_assistant"
    / "recipes"
    / "imported"
)
RECIPE_ID_PREFIX = "video_"
_NUMBER_RE = re.compile(r"(?<![A-Za-z])(?P<number>\d+/\d+|\d+(?:\.\d+)?)(?![A-Za-z])")
_SAFE_RECIPE_ID = re.compile(r"^video_[A-Za-z0-9][A-Za-z0-9_.-]{1,127}$")


class VideoRecipeStoreError(ValueError):
    pass


class VideoRecipeStore:
    """Store one JSON file per confirmed imported recipe.

    The store never runs the normalizer while reading.  The review pipeline
    standardizes the draft after import and after each PATCH; confirmation
    only validates and persists that displayed version.  Reads only scale
    numeric ingredient quantities and their matching step quantities for the
    requested serving count.
    """

    def __init__(self, store_dir: str | Path | None = None) -> None:
        self.store_dir = Path(store_dir) if store_dir is not None else DEFAULT_IMPORTED_RECIPE_DIR
        self.store_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def save(self, recipe: dict[str, Any], *, recipe_id: str | None = None) -> dict[str, Any]:
        if not isinstance(recipe, dict):
            raise VideoRecipeStoreError("菜谱必须是对象。")
        payload = deepcopy(recipe)
        selected_id = str(recipe_id or payload.get("recipe_id") or "").strip()
        if not selected_id:
            selected_id = f"{RECIPE_ID_PREFIX}{uuid.uuid4().hex}"
        if not selected_id.startswith(RECIPE_ID_PREFIX):
            selected_id = f"{RECIPE_ID_PREFIX}{selected_id}"
        self._validate_recipe_id(selected_id)
        payload["recipe_id"] = selected_id
        self._validate_recipe(payload)
        path = self._path_for(selected_id)
        with self._lock:
            self.store_dir.mkdir(parents=True, exist_ok=True)
            # Keep the temporary file in the same directory so os.replace is
            # atomic on the filesystem used by the runtime.
            temporary: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    dir=self.store_dir,
                    prefix=f".{selected_id}.",
                    suffix=".tmp",
                    delete=False,
                ) as handle:
                    temporary = Path(handle.name)
                    json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, path)
            finally:
                if temporary is not None and temporary.exists():
                    try:
                        temporary.unlink()
                    except OSError:
                        pass
        return deepcopy(payload)

    def get(self, recipe_id: str) -> dict[str, Any]:
        selected_id = self._validate_recipe_id(recipe_id)
        path = self._path_for(selected_id)
        with self._lock:
            if not path.exists() or not path.is_file():
                raise KeyError("菜谱不存在。")
            try:
                with path.open("r", encoding="utf-8") as handle:
                    payload = json.load(handle)
            except (OSError, json.JSONDecodeError) as exc:
                raise VideoRecipeStoreError("已保存菜谱无法读取。") from exc
        if not isinstance(payload, dict):
            raise VideoRecipeStoreError("已保存菜谱格式无效。")
        self._validate_recipe(payload)
        return deepcopy(payload)

    def list_recipes(self) -> list[dict[str, Any]]:
        with self._lock:
            paths = sorted(
                (item for item in self.store_dir.glob("video_*.json") if item.is_file()),
                key=lambda item: item.name,
            )
            result: list[dict[str, Any]] = []
            for path in paths:
                try:
                    with path.open("r", encoding="utf-8") as handle:
                        payload = json.load(handle)
                except (OSError, json.JSONDecodeError):
                    continue
                if not isinstance(payload, dict):
                    continue
                try:
                    self._validate_recipe(payload)
                except VideoRecipeStoreError:
                    continue
                result.append(self._summary(payload))
            return deepcopy(result)

    def delete(self, recipe_id: str) -> None:
        selected_id = self._validate_recipe_id(recipe_id)
        path = self._path_for(selected_id)
        with self._lock:
            try:
                path.unlink()
            except FileNotFoundError:
                return
            except OSError as exc:
                raise VideoRecipeStoreError("已保存菜谱无法删除。") from exc

    def detail(self, recipe_id: str, servings: int = 1) -> dict[str, Any]:
        payload = self.get(recipe_id)
        if isinstance(servings, bool) or not isinstance(servings, int) or servings < 1 or servings > 6:
            raise VideoRecipeStoreError("用餐人数必须在1到6人之间。")
        target = servings
        return scale_recipe(payload, target)

    # Compatibility aliases make the store convenient for console adapters.
    get_recipe = get
    recipe_detail = detail
    list = list_recipes

    def _path_for(self, recipe_id: str) -> Path:
        selected_id = self._validate_recipe_id(recipe_id)
        return self.store_dir / f"{selected_id}.json"

    @staticmethod
    def _validate_recipe_id(recipe_id: str) -> str:
        selected_id = str(recipe_id or "").strip()
        if not _SAFE_RECIPE_ID.fullmatch(selected_id):
            raise VideoRecipeStoreError("菜谱标识无效。")
        return selected_id

    @staticmethod
    def _validate_recipe(recipe: dict[str, Any]) -> None:
        recipe_id = str(recipe.get("recipe_id") or "")
        if not _SAFE_RECIPE_ID.fullmatch(recipe_id):
            raise VideoRecipeStoreError("菜谱标识无效。")
        if not str(recipe.get("name") or "").strip():
            raise VideoRecipeStoreError("菜名不能为空。")
        servings = recipe.get("servings")
        if isinstance(servings, bool) or not isinstance(servings, int) or servings <= 0:
            raise VideoRecipeStoreError("菜谱人数无效。")
        ingredients = recipe.get("ingredients")
        if not isinstance(ingredients, list) or not ingredients:
            raise VideoRecipeStoreError("菜谱必须包含食材。")
        steps = recipe.get("steps")
        if not isinstance(steps, list) or not steps:
            raise VideoRecipeStoreError("菜谱必须包含步骤。")
        metadata = recipe.get("import_metadata")
        if not isinstance(metadata, dict):
            raise VideoRecipeStoreError("导入菜谱缺少来源记录。")
        if metadata.get("confirmed") is not True:
            raise VideoRecipeStoreError("导入菜谱尚未确认。")
        source = metadata.get("source")
        if not isinstance(source, dict) or not str(source.get("platform") or "").strip():
            raise VideoRecipeStoreError("导入菜谱来源记录无效。")
        for item in ingredients:
            if not isinstance(item, dict) or not str(item.get("name") or "").strip():
                raise VideoRecipeStoreError("食材字段无效。")
            if item.get("amount") in (None, ""):
                raise VideoRecipeStoreError("食材用量无效。")
            if not str(item.get("unit") or "").strip():
                raise VideoRecipeStoreError("食材单位无效。")
            if "optional" in item and not isinstance(item.get("optional"), bool):
                raise VideoRecipeStoreError("食材optional字段无效。")
        for index, step in enumerate(steps, start=1):
            if not isinstance(step, dict) or not str(step.get("instruction") or "").strip():
                raise VideoRecipeStoreError("步骤字段无效。")
            duration = step.get("duration_seconds")
            if duration is not None:
                if isinstance(duration, bool) or not isinstance(duration, (int, float)):
                    raise VideoRecipeStoreError(f"第{index}步计时字段无效。")
                if not math.isfinite(float(duration)) or duration <= 0:
                    raise VideoRecipeStoreError(f"第{index}步计时字段无效。")

    @staticmethod
    def _summary(recipe: dict[str, Any]) -> dict[str, Any]:
        metadata = recipe.get("import_metadata") if isinstance(recipe.get("import_metadata"), dict) else {}
        source = metadata.get("source") if isinstance(metadata.get("source"), dict) else {}
        return {
            "recipe_id": recipe.get("recipe_id"),
            "name": recipe.get("name"),
            "servings": recipe.get("servings"),
            "estimated_time_minutes": recipe.get("estimated_time_minutes"),
            "difficulty": recipe.get("difficulty"),
            "source_name": recipe.get("source_name") or source.get("platform"),
            "source_url": source.get("source_url"),
            "imported": True,
        }


def _format_number(value: Fraction) -> str:
    if value.denominator == 1:
        return str(value.numerator)
    if abs(value) < 1 and value.denominator in {2, 3, 4, 8}:
        return f"{value.numerator}/{value.denominator}"
    return f"{float(value):.2f}".rstrip("0").rstrip(".")


def scale_amount(amount: Any, factor: Fraction) -> Any:
    """Scale numeric tokens while preserving a string amount's units."""

    if isinstance(amount, bool):
        return amount
    if isinstance(amount, int):
        return int(Fraction(amount) * factor) if (Fraction(amount) * factor).denominator == 1 else _format_number(Fraction(amount) * factor)
    if isinstance(amount, float) and math.isfinite(amount):
        scaled = Fraction(str(amount)) * factor
        return int(scaled) if scaled.denominator == 1 else float(scaled)
    text = str(amount or "")
    if not text:
        return text

    def replace(match: re.Match[str]) -> str:
        token = match.group("number")
        try:
            value = Fraction(token)
        except (ValueError, ZeroDivisionError):
            return token
        return _format_number(value * factor)

    return _NUMBER_RE.sub(replace, text)


def _amount_tokens(amount: Any) -> list[str]:
    if isinstance(amount, bool):
        return []
    if isinstance(amount, (int, float)):
        return [str(amount)]
    return [match.group("number") for match in _NUMBER_RE.finditer(str(amount or ""))]


def _replace_step_amounts(
    instruction: str,
    replacements: list[tuple[str, str, str | None]],
) -> str:
    result = str(instruction or "")
    # Build one alternation and replace in one pass.  Repeated substitutions
    # can cascade (200 -> 300 followed by 300 -> 450) when two ingredients
    # share a unit; the callback always sees the original instruction.
    unique: dict[tuple[str, str | None], set[str]] = {}
    for source, target, unit in replacements:
        if source and source != target:
            unique.setdefault((source, unit), set()).add(target)
    patterns: list[tuple[str, str, str | None]] = []
    for (source, unit), targets in unique.items():
        # If two ingredients have the same source amount and unit but scale to
        # different values, changing that number would be ambiguous.  Leave it
        # untouched and let the ingredient list remain the source of truth.
        if len(targets) != 1:
            continue
        target = next(iter(targets))
        patterns.append((source, target, unit))
    if not patterns:
        return result
    alternatives: list[str] = []
    lookup: dict[str, tuple[str, str | None]] = {}
    for index, (source, target, unit) in enumerate(sorted(patterns, key=lambda item: len(item[0]), reverse=True)):
        token = f"__amount_{index}__"
        if unit:
            pattern = rf"(?<![\d./]){re.escape(source)}(?=\s*{re.escape(unit)})"
        else:
            pattern = rf"(?<![\d./]){re.escape(source)}(?![\d./])"
        alternatives.append(f"(?P<{token}>{pattern})")
        lookup[token] = (target, unit)
    combined = re.compile("|".join(alternatives))

    def replace(match: re.Match[str]) -> str:
        for token, (target, _unit) in lookup.items():
            if match.group(token) is not None:
                return target
        return match.group(0)

    return combined.sub(replace, result)


def scale_recipe(recipe: dict[str, Any], servings: int) -> dict[str, Any]:
    """Return a read-time serving-scaled copy without workflow mutation."""

    if not isinstance(recipe, dict):
        raise VideoRecipeStoreError("菜谱必须是对象。")
    source_servings = recipe.get("servings")
    if isinstance(source_servings, bool) or not isinstance(source_servings, int) or source_servings <= 0:
        raise VideoRecipeStoreError("菜谱人数无效。")
    target = int(servings)
    if isinstance(servings, bool) or not isinstance(servings, int) or target <= 0 or target > 6:
        raise VideoRecipeStoreError("用餐人数必须在1到6人之间。")
    if target == source_servings:
        copy = deepcopy(recipe)
        copy.setdefault("import_metadata", {})
        copy["import_metadata"] = deepcopy(copy["import_metadata"])
        return copy
    factor = Fraction(target, source_servings)
    scaled = deepcopy(recipe)
    scaled["servings"] = target
    replacements: list[tuple[str, str, str | None]] = []
    ingredients = scaled.get("ingredients") if isinstance(scaled.get("ingredients"), list) else []
    source_ingredients = recipe.get("ingredients") if isinstance(recipe.get("ingredients"), list) else []
    for index, item in enumerate(ingredients):
        if not isinstance(item, dict):
            continue
        before = source_ingredients[index] if index < len(source_ingredients) and isinstance(source_ingredients[index], dict) else {}
        source_amount = before.get("amount")
        item["amount"] = scale_amount(source_amount, factor)
        before_tokens = _amount_tokens(source_amount)
        after_tokens = _amount_tokens(item["amount"])
        unit = str(before.get("unit") or "").strip() or None
        for source_token, target_token in zip(before_tokens, after_tokens):
            replacements.append((source_token, target_token, unit))
    steps = scaled.get("steps") if isinstance(scaled.get("steps"), list) else []
    for step in steps:
        if isinstance(step, dict):
            step["instruction"] = _replace_step_amounts(str(step.get("instruction") or ""), replacements)
    metadata = scaled.get("import_metadata")
    if isinstance(metadata, dict):
        metadata["display_servings"] = target
        metadata["scaled_from_servings"] = source_servings
    return scaled


__all__ = [
    "DEFAULT_IMPORTED_RECIPE_DIR",
    "RECIPE_ID_PREFIX",
    "VideoRecipeStore",
    "VideoRecipeStoreError",
    "scale_amount",
    "scale_recipe",
]
