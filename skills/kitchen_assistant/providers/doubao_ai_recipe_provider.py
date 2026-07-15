from __future__ import annotations

import re
import threading
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from typing import Any

from kitchen.dietary_rules import ingredient_conflicts
from kitchen.dish_profiles import matching_profiles
from kitchen.models import RecipeCandidate, RecipeSearchRequest, split_main_foods_and_seasonings
from kitchen.recipe_normalizer import RecipeNormalizer
from llm.doubao_client import DoubaoClientError, DoubaoLLMClient
from llm.prompts import candidate_messages, recipe_messages


class AIRecipeProviderError(RuntimeError):
    pass


class DoubaoAIRecipeProvider:
    """Generate recipes with Doubao Chat Completions, never web search."""

    supports_ai = True

    def __init__(self, llm_client: DoubaoLLMClient, fallback: Any) -> None:
        self.llm_client = llm_client
        self.fallback = fallback
        self._candidate_requests: dict[str, RecipeSearchRequest] = {}
        self._detail_cache: dict[str, dict[str, Any]] = {}
        self._local_candidate_ids: set[str] = set()
        self._mode = "ai_generated"
        self.last_cache_path: Path | None = None
        self.last_cache_paths: list[Path] = []
        self.last_cache_candidate_count = 0
        self._last_cached_candidate_ids: set[str] = set()
        self._cache_lock = threading.Lock()

    @property
    def mode(self) -> str:
        return self._mode

    def search_recipes(self, request: RecipeSearchRequest) -> list[RecipeCandidate]:
        self.last_cache_path = None
        self.last_cache_paths = []
        self.last_cache_candidate_count = 0
        self._last_cached_candidate_ids = set()
        cached_search = getattr(self.fallback, "search_cached_recipes", None)
        if callable(cached_search):
            cached = cached_search(request)
            if cached:
                self._mode = "local_cache"
                self._local_candidate_ids.update(candidate.candidate_id for candidate in cached)
                return cached[:3]
        self._mode = "ai_generated"
        if not self.llm_client.is_available():
            raise AIRecipeProviderError("豆包未配置")
        try:
            payload = self.llm_client.generate_json(candidate_messages(_request_payload(request)))
            rows = payload.get("candidates")
            if not isinstance(rows, list) or not rows:
                raise AIRecipeProviderError("候选菜谱为空")
            candidates = [_candidate_from_row(row, index, request) for index, row in enumerate(rows[:3], start=1)]
            if not candidates:
                raise AIRecipeProviderError("没有有效候选菜谱")
            if request.requested_dish and request.requested_dish not in candidates[0].title:
                raise AIRecipeProviderError("第一个候选没有保留用户指定菜名")
            for candidate in candidates:
                self._candidate_requests[candidate.candidate_id] = deepcopy(request)
            # Generate and persist all three details while the request is
            # active. A later choice can then use memory or the JSON cache
            # without another model call.
            # Detail calls are independent. Keep all three candidates cached,
            # but overlap network latency so a recommendation round waits for
            # roughly one model response instead of three sequential ones.
            with ThreadPoolExecutor(max_workers=min(3, len(candidates))) as pool:
                futures = [pool.submit(self.get_recipe_detail, candidate) for candidate in candidates]
                for future in futures:
                    try:
                        future.result()
                    except Exception:
                        # One malformed detail must not hide the other valid
                        # candidates; the selected item can be retried later.
                        continue
            return candidates
        except (DoubaoClientError, ValueError, TypeError, AIRecipeProviderError) as exc:
            raise AIRecipeProviderError("豆包候选菜谱生成失败") from exc

    def get_recipe_detail(self, candidate: RecipeCandidate) -> dict[str, Any]:
        if candidate.candidate_id in self._local_candidate_ids:
            self._mode = "local_cache"
            return self.fallback.get_recipe_detail(candidate)
        cached_detail = self._detail_cache.get(candidate.candidate_id)
        if cached_detail is not None:
            self._mode = "ai_generated"
            return deepcopy(cached_detail)
        self._mode = "ai_generated"
        request = self._candidate_requests.get(candidate.candidate_id)
        if request is None:
            raise AIRecipeProviderError("候选菜谱上下文已失效")
        try:
            raw = self.llm_client.generate_json(recipe_messages(candidate.as_dict(), _request_payload(request)))
            _ensure_profile_ingredients(raw, request)
            _ensure_inventory_ingredients(raw, request)
            _normalize_profile_timers(raw, request)
            _validate_raw_recipe(raw)
            raw["recipe_id"] = candidate.candidate_id
            raw["name"] = str(raw.get("title") or candidate.title).strip()
            raw.pop("title", None)
            raw["source_name"] = "豆包 AI 生成"
            raw["source_url"] = None
            raw["notes"] = _string_list(raw.pop("safety_notes", []))
            self._save_normalized_cache(raw, candidate, request)
            self._detail_cache[candidate.candidate_id] = deepcopy(raw)
            return raw
        except Exception as exc:
            raise AIRecipeProviderError("豆包完整菜谱生成失败") from exc

    def fallback_recipe(self) -> dict[str, Any]:
        return self.fallback.fallback_recipe()

    def _save_normalized_cache(
        self,
        raw: dict[str, Any],
        candidate: RecipeCandidate,
        request: RecipeSearchRequest,
    ) -> None:
        save = getattr(self.fallback, "save_generated_recipe", None)
        if not callable(save):
            return
        try:
            normalized = RecipeNormalizer().normalize(raw)
            normalized["summary"] = candidate.summary
            with self._cache_lock:
                saved = save(normalized, request)
            self.last_cache_path = saved if isinstance(saved, Path) else None
            self._last_cached_candidate_ids.add(candidate.candidate_id)
            self.last_cache_candidate_count = len(self._last_cached_candidate_ids)
            if isinstance(saved, Path) and saved not in self.last_cache_paths:
                self.last_cache_paths.append(saved)
        except (OSError, ValueError, TypeError):
            # Cache failures must never stop an otherwise valid cooking flow.
            return


def _candidate_from_row(row: Any, index: int, request: RecipeSearchRequest) -> RecipeCandidate:
    if not isinstance(row, dict):
        raise ValueError("候选必须是对象")
    title = _text(row.get("title"), "菜名")
    summary = _text(row.get("summary"), "摘要")
    difficulty = str(row.get("difficulty") or "简单").strip()
    if difficulty not in {"简单", "中等"}:
        difficulty = "简单"
    minutes = row.get("estimated_minutes")
    if not isinstance(minutes, int) or not 1 <= minutes <= 240:
        raise ValueError("预计时间无效")
    labels = _string_list(row.get("main_ingredients"))
    seasonings = _string_list(row.get("main_seasonings"))
    if not labels:
        raise ValueError("候选缺少主要食材")
    for profile in matching_profiles(request.requested_dish, title):
        _append_missing(labels, [str(item) for item in profile.get("candidate_ingredients", []) if str(item)])
    ingredients, inferred_seasonings = split_main_foods_and_seasonings(labels)
    _, declared_seasonings = split_main_foods_and_seasonings(seasonings)
    _append_missing(inferred_seasonings, declared_seasonings)
    # No pantry declaration means “unknown”, rather than “everything missing”.
    missing = _string_list(row.get("missing_ingredients")) if request.available_ingredients else []
    if ingredient_conflicts([*ingredients, *inferred_seasonings], request.dietary_restrictions):
        raise ValueError("候选违反忌口")
    if request.requested_dish and index == 1 and request.requested_dish not in title:
        raise ValueError("候选没有保留指定菜名")
    if not request.requested_dish and request.available_ingredients:
        labels_for_matching = [*ingredients, *inferred_seasonings]
        missing_inventory = [
            item for item in request.available_ingredients
            if not _inventory_ingredient_present(item, labels_for_matching)
        ]
        if missing_inventory:
            raise ValueError(f"候选遗漏用户已有食材：{'、'.join(missing_inventory)}")
    candidate_id = f"ai_{_slug(title)}_{index}"
    return RecipeCandidate(
        candidate_id=candidate_id,
        title=title,
        source_name="豆包 AI 生成",
        source_url=None,
        summary=summary,
        estimated_minutes=minutes,
        difficulty=difficulty,
        main_ingredients=ingredients,
        missing_ingredients=missing,
        match_reason=_text(row.get("match_reason"), "匹配说明"),
        main_seasonings=inferred_seasonings,
    )


_INVENTORY_ALIASES = {
    "蘑菇": ("蘑菇", "香菇", "口蘑", "平菇", "白玉菇"),
    "牛肉": ("牛肉", "肥牛", "牛腩", "牛里脊"),
}


def _inventory_ingredient_present(required: str, labels: list[str]) -> bool:
    aliases = _INVENTORY_ALIASES.get(required, (required,))
    return any(alias in label or label in alias for alias in aliases for label in labels)


def _ensure_inventory_ingredients(raw: Any, request: RecipeSearchRequest) -> None:
    """Do not let a detail response silently drop pantry food from its candidate."""
    if request.requested_dish or not request.available_ingredients or not isinstance(raw, dict):
        return
    ingredient_rows = raw.get("ingredients")
    if not isinstance(ingredient_rows, list):
        return
    labels = [str(item.get("name", "")) for item in ingredient_rows if isinstance(item, dict)]
    missing = [item for item in request.available_ingredients if not _inventory_ingredient_present(item, labels)]
    if missing:
        raise ValueError(f"完整菜谱遗漏用户已有食材：{'、'.join(missing)}")


def _validate_raw_recipe(raw: Any) -> None:
    if not isinstance(raw, dict):
        raise ValueError("菜谱必须是对象")
    if not _text(raw.get("title"), "菜名"):
        raise ValueError("菜名不能为空")
    ingredients = raw.get("ingredients")
    if not isinstance(ingredients, list) or not ingredients:
        raise ValueError("菜谱必须包含食材")
    for item in ingredients:
        if not isinstance(item, dict) or not _text(item.get("name"), "食材"):
            raise ValueError("食材无效")
        amount = _text(item.get("amount"), "")
        if not amount or any(marker in amount for marker in ("适量", "少量", "少许", "按口味")):
            raise ValueError("食材必须给出明确用量")
    steps = raw.get("steps")
    if not isinstance(steps, list) or not steps:
        raise ValueError("菜谱必须包含步骤")
    for index, step in enumerate(steps, start=1):
        if not isinstance(step, dict) or not _text(step.get("instruction"), "步骤"):
            raise ValueError("步骤说明无效")
        instruction = _text(step.get("instruction"), "步骤")
        if any(marker in instruction for marker in ("准备调料碗", "准备调料", "准备酱汁")):
            concrete_seasonings = ("生抽", "老抽", "米醋", "醋", "白砂糖", "白糖", "盐", "料酒", "蚝油", "胡椒")
            if not any(marker in instruction for marker in concrete_seasonings):
                raise ValueError("调料准备步骤必须写明具体调料和用量")
        # The provider owns a public data shape, but sequential numbering is
        # enforced here before the normalizer maps multimodal fields.
        step["step_number"] = index
        if step.get("duration_seconds") is not None and not isinstance(step["duration_seconds"], (int, float)):
            step["duration_seconds"] = None
        if step.get("heat_level") is not None and not isinstance(step["heat_level"], str):
            step["heat_level"] = None
        if step.get("safety_note") is not None and not isinstance(step["safety_note"], str):
            step["safety_note"] = None


def _request_payload(request: RecipeSearchRequest) -> dict[str, Any]:
    return {
        "requested_dish": request.requested_dish,
        "available_ingredients": request.available_ingredients,
        "servings": request.servings,
        "taste_preferences": request.taste_preferences,
        "dietary_restrictions": request.dietary_restrictions,
        "max_cooking_minutes": request.max_cooking_minutes,
        "available_equipment": request.available_equipment,
        "difficulty_preference": request.difficulty_preference,
        "steak_doneness": request.steak_doneness,
        "steak_thickness_cm": request.steak_thickness_cm,
    }


def _normalize_profile_timers(raw: Any, request: RecipeSearchRequest) -> None:
    """Apply cautious, data-defined timing bounds for matching dish profiles."""
    if not isinstance(raw, dict):
        return
    steps = raw.get("steps")
    if not isinstance(steps, list):
        return
    for profile in matching_profiles(request.requested_dish, raw.get("title")):
        policy = profile.get("sear_timer")
        if not isinstance(policy, dict):
            continue
        thickness = getattr(request, str(policy.get("thickness_field", "")), None) or float(policy.get("default_thickness_cm", 2.0))
        maximum = next((int(limit) for threshold, limit in policy.get("maximum_seconds", []) if thickness <= float(threshold)), 150)
        option = getattr(request, str(policy.get("option_field", "")), None)
        base = int((policy.get("base_seconds_by_option") or {}).get(option or "", 60))
        default_seconds = max(int(policy.get("minimum_seconds", 30)), min(maximum, base + round((float(thickness) - 2.0) * int(policy.get("per_cm_adjustment_seconds", 20)))))
        for step in steps:
            if not isinstance(step, dict):
                continue
            instruction = str(step.get("instruction", ""))
            markers = tuple(str(marker) for marker in policy.get("side_markers", []) if str(marker))
            if str(policy.get("action_marker", "")) not in instruction or not any(marker in instruction for marker in markers):
                continue
            duration = step.get("duration_seconds")
            if not isinstance(duration, (int, float)) or duration <= 0 or duration > maximum:
                duration = default_seconds
                step["duration_seconds"] = duration
            seconds = int(duration)
            instruction = re.sub(r"(?:约|大约)?\s*\d+(?:\.\d+)?\s*(?:分钟|min)", f"约 {seconds} 秒", instruction, flags=re.IGNORECASE)
            if "初始参考" not in instruction:
                instruction = f"{instruction}（{str(policy.get('instruction_suffix', '')).format(seconds=seconds)}）"
            step["instruction"] = instruction
            safety_note = str(step.get("safety_note") or "")
            reminder = str(policy.get("safety_note") or "")
            if reminder and reminder not in safety_note:
                step["safety_note"] = f"{safety_note} {reminder}".strip()


def _ensure_profile_ingredients(raw: Any, request: RecipeSearchRequest) -> None:
    """Ensure generated recipes include the basics declared by their profile."""
    if not isinstance(raw, dict):
        return
    ingredients = raw.get("ingredients")
    if not isinstance(ingredients, list):
        return
    names = [str(item.get("name", "")) for item in ingredients if isinstance(item, dict)]
    for profile in matching_profiles(request.requested_dish, raw.get("title")):
        for item in profile.get("default_ingredients", []):
            if isinstance(item, dict) and not any(str(item.get("name", "")) in name or name in str(item.get("name", "")) for name in names):
                ingredients.append(dict(item))
                names.append(str(item.get("name", "")))


def _append_missing(values: list[str], expected: list[str]) -> None:
    for item in expected:
        if not any(item in value or value in item for value in values):
            values.append(item)


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if isinstance(item, str) and item.strip()][:12]


def _text(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 120:
        raise ValueError(f"{label}无效")
    return text


def _slug(text: str) -> str:
    value = re.sub(r"[^\w\u4e00-\u9fff]+", "_", text).strip("_")
    return value[:40] or "recipe"
