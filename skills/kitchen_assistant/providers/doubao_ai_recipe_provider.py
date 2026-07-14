from __future__ import annotations

import re
from copy import deepcopy
from pathlib import Path
from typing import Any

from kitchen.dietary_rules import ingredient_conflicts
from kitchen.models import RecipeCandidate, RecipeSearchRequest
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

    @property
    def mode(self) -> str:
        return self._mode

    def search_recipes(self, request: RecipeSearchRequest) -> list[RecipeCandidate]:
        self.last_cache_path = None
        self.last_cache_paths = []
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
            for candidate in candidates:
                try:
                    self.get_recipe_detail(candidate)
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
            _ensure_steak_ingredients(raw, request)
            _normalize_steak_sear_durations(raw, request)
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
            saved = save(normalized, request)
            self.last_cache_path = saved if isinstance(saved, Path) else None
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
    ingredients = _string_list(row.get("main_ingredients"))
    if not ingredients:
        raise ValueError("候选缺少主要食材")
    if "牛排" in str(request.requested_dish or title):
        _append_missing(ingredients, ["牛排", "精炼橄榄油", "黄油", "盐", "黑胡椒"])
    # No pantry declaration means “unknown”, rather than “everything missing”.
    missing = _string_list(row.get("missing_ingredients")) if request.available_ingredients else []
    if ingredient_conflicts(ingredients, request.dietary_restrictions):
        raise ValueError("候选违反忌口")
    if request.requested_dish and index == 1 and request.requested_dish not in title:
        raise ValueError("候选没有保留指定菜名")
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
    )


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
    steps = raw.get("steps")
    if not isinstance(steps, list) or not steps:
        raise ValueError("菜谱必须包含步骤")
    for index, step in enumerate(steps, start=1):
        if not isinstance(step, dict) or not _text(step.get("instruction"), "步骤"):
            raise ValueError("步骤说明无效")
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


def _normalize_steak_sear_durations(raw: Any, request: RecipeSearchRequest) -> None:
    """Bound AI steak timers to cautious initial sear windows.

    The model supplies the recipe, but local code guards against a generic
    “two minutes per side” default when the user supplied an ordinary-thickness
    steak. These are initial timings only; the step text keeps the required
    temperature/centre check instead of claiming a guaranteed doneness.
    """
    if not isinstance(raw, dict) or "牛排" not in str(request.requested_dish or raw.get("title", "")):
        return
    steps = raw.get("steps")
    if not isinstance(steps, list):
        return
    thickness = request.steak_thickness_cm or 2.0
    if thickness <= 2.2:
        maximum = 90
    elif thickness <= 3.0:
        maximum = 120
    else:
        maximum = 150
    default_seconds = _default_steak_sear_seconds(request.steak_doneness, thickness, maximum)
    for step in steps:
        if not isinstance(step, dict):
            continue
        instruction = str(step.get("instruction", ""))
        is_side = "煎" in instruction and any(word in instruction for word in ("正面", "翻面", "反面", "另一面"))
        if not is_side:
            continue
        duration = step.get("duration_seconds")
        if not isinstance(duration, (int, float)) or duration <= 0 or duration > maximum:
            duration = default_seconds
            step["duration_seconds"] = duration
        seconds = int(duration)
        # Do not let an instruction contradict its structured timer.
        instruction = re.sub(r"(?:约|大约)?\s*\d+(?:\.\d+)?\s*(?:分钟|min)", f"约 {seconds} 秒", instruction, flags=re.IGNORECASE)
        if "初始参考" not in instruction:
            instruction = f"{instruction}（约 {seconds} 秒为初始参考，按中心温度和上色情况调整。）"
        step["instruction"] = instruction
        safety_note = str(step.get("safety_note") or "")
        if "温度计" not in safety_note:
            step["safety_note"] = (safety_note + " 使用食品温度计或切开中心检查，不以计时保证熟度。").strip()


def _ensure_steak_ingredients(raw: Any, request: RecipeSearchRequest) -> None:
    """Ensure a generated steak recipe does not omit basic seasoning/oil."""
    if not isinstance(raw, dict) or "牛排" not in str(request.requested_dish or raw.get("title", "")):
        return
    ingredients = raw.get("ingredients")
    if not isinstance(ingredients, list):
        return
    names = [str(item.get("name", "")) for item in ingredients if isinstance(item, dict)]
    defaults = [
        {"name": "牛排", "amount": 1, "unit": "块", "optional": False},
        {"name": "精炼橄榄油", "amount": 1, "unit": "茶匙", "optional": False},
        {"name": "盐", "amount": "1/4", "unit": "茶匙", "optional": False},
        {"name": "黑胡椒", "amount": "1/4", "unit": "茶匙", "optional": False},
        {"name": "黄油", "amount": 10, "unit": "克", "optional": True},
        {"name": "蒜", "amount": 1, "unit": "瓣", "optional": True},
        {"name": "迷迭香", "amount": 1, "unit": "小枝", "optional": True},
    ]
    for item in defaults:
        if not any(item["name"] in name or name in item["name"] for name in names):
            ingredients.append(item)
            names.append(item["name"])


def _default_steak_sear_seconds(doneness: str | None, thickness_cm: float, maximum: int) -> int:
    base_for_two_cm = {"三分熟": 45, "五分熟": 60, "七分熟": 75, "全熟": 90}
    base = base_for_two_cm.get(doneness or "", 60)
    adjusted = base + round((thickness_cm - 2.0) * 20)
    return max(30, min(maximum, adjusted))


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
