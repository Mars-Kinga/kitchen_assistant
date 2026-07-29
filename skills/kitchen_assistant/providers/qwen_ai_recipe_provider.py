from __future__ import annotations

import re
import threading
import time
from copy import deepcopy
from pathlib import Path
from typing import Any

from kitchen.dietary_rules import ingredient_conflicts
from kitchen.dish_profiles import matching_profiles
from kitchen.ingredient_vocabulary import (
    INGREDIENT_GROUPS,
    ingredient_matches,
    ingredient_present,
    split_main_foods_and_seasonings,
)
from kitchen.models import RecipeCandidate, RecipeSearchRequest
from kitchen.recipe_contract import (
    candidate_limit,
    string_list as _string_list,
    validate_estimated_minutes,
    validate_raw_recipe,
    validate_text as _text,
)
from kitchen.recipe_normalizer import RecipeNormalizer
from llm.qwen_client import QwenClientError, QwenJSONOutputError, QwenLLMClient
from llm.prompts import recipe_bundle_messages, recipe_correction_messages


class AIRecipeProviderError(RuntimeError):
    pass


# Keep enough headroom for one complete 6–10 step JSON recipe. The prompt asks
# for concise fields, so this is a ceiling rather than a target; lowering it to
# 2600 caused some valid long recipes to be cut off mid-JSON.
RECIPE_BUNDLE_MAX_TOKENS = 3600
MAX_CORRECTION_TIMEOUT_SECONDS = 8.0
MIN_CORRECTION_BUDGET_SECONDS = 2.0


class QwenAIRecipeProvider:
    """Generate recipes with Qwen Chat Completions, never web search."""

    supports_ai = True

    def __init__(
        self,
        llm_client: QwenLLMClient,
        fallback: Any,
        *,
        clock: Any = time.monotonic,
    ) -> None:
        self.llm_client = llm_client
        self.fallback = fallback
        self._detail_cache: dict[str, dict[str, Any]] = {}
        self._local_candidate_ids: set[str] = set()
        self._mode = "ai_generated"
        self.used_local_first = False
        self.last_cache_path: Path | None = None
        self.last_cache_paths: list[Path] = []
        self.last_cache_candidate_count = 0
        self._last_cached_candidate_ids: set[str] = set()
        self._cache_lock = threading.Lock()
        self._clock = clock

    @property
    def mode(self) -> str:
        return self._mode

    def search_recipes(self, request: RecipeSearchRequest) -> list[RecipeCandidate]:
        self.last_cache_path = None
        self.last_cache_paths = []
        self.last_cache_candidate_count = 0
        self._last_cached_candidate_ids = set()
        self.used_local_first = False
        request_payload = _request_payload(request)
        limit = candidate_limit(request_payload)
        local_search = getattr(self.fallback, "search_recipes", None)
        if callable(local_search):
            local = local_search(request)
            if local:
                # The fixed catalog and validated generated cache are both
                # authoritative local sources. Never spend a cloud request
                # merely to replace or pad an existing local result.
                self._mode = (
                    "local_cache"
                    if all(candidate.source_name == "已保存菜谱" for candidate in local)
                    else "mock"
                )
                self.used_local_first = True
                self._local_candidate_ids.update(candidate.candidate_id for candidate in local)
                return local
        self._mode = "ai_generated"
        if not self.llm_client.is_available():
            raise AIRecipeProviderError("千问未配置")
        started_at = self._clock()
        try:
            payload: dict[str, Any] | None = None
            try:
                payload = self.llm_client.generate_json(
                    recipe_bundle_messages(request_payload),
                    max_tokens=RECIPE_BUNDLE_MAX_TOKENS,
                    timeout=self._total_budget_seconds(),
                )
                prepared = _prepare_bundle_payload(payload, request, limit)
            except QwenJSONOutputError as exc:
                prepared = self._correct_bundle_once(
                    request_payload,
                    request,
                    limit,
                    invalid_output=exc.raw_content,
                    original_error=exc,
                    started_at=started_at,
                )
            except (ValueError, TypeError, AIRecipeProviderError) as exc:
                if not _is_correctable_recipe_error(exc):
                    raise
                prepared = self._correct_bundle_once(
                    request_payload,
                    request,
                    limit,
                    invalid_output=payload or {},
                    original_error=exc,
                    started_at=started_at,
                )
            candidates = [candidate for candidate, _ in prepared]
            for candidate, raw in prepared:
                self._detail_cache[candidate.candidate_id] = deepcopy(raw)
                self._save_normalized_cache(raw, candidate, request)
            return candidates
        except (QwenClientError, ValueError, TypeError, AIRecipeProviderError) as exc:
            raise AIRecipeProviderError("千问候选菜谱生成失败") from exc

    def _correct_bundle_once(
        self,
        request_payload: dict[str, Any],
        request: RecipeSearchRequest,
        limit: int,
        *,
        invalid_output: Any,
        original_error: Exception,
        started_at: float,
    ) -> list[tuple[RecipeCandidate, dict[str, Any]]]:
        remaining = self._total_budget_seconds() - (self._clock() - started_at)
        if remaining < MIN_CORRECTION_BUDGET_SECONDS:
            raise original_error
        timeout = min(MAX_CORRECTION_TIMEOUT_SECONDS, remaining)
        issue = _validation_issue(original_error)
        print(f"[厨房助手-菜谱纠错] 首次结果未通过：{issue['code']}，尝试定向修正一次")
        try:
            corrected = self.llm_client.generate_json(
                recipe_correction_messages(request_payload, invalid_output, issue),
                max_tokens=RECIPE_BUNDLE_MAX_TOKENS,
                timeout=timeout,
            )
            prepared = _prepare_bundle_payload(corrected, request, limit)
        except (QwenClientError, ValueError, TypeError, AIRecipeProviderError) as exc:
            print(f"[厨房助手-菜谱纠错] 修正失败：{_validation_issue(exc)['code']}")
            raise AIRecipeProviderError("千问菜谱定向纠错失败") from exc
        print("[厨房助手-菜谱纠错] 修正结果校验通过")
        return prepared

    def _total_budget_seconds(self) -> float:
        return max(float(getattr(self.llm_client, "timeout", 25.0)), 0.1)

    def has_local_match(self, request: RecipeSearchRequest) -> bool:
        """Allow the session to avoid showing cloud-generation progress."""
        local_search = getattr(self.fallback, "search_recipes", None)
        return bool(callable(local_search) and local_search(request))

    def get_recipe_detail(self, candidate: RecipeCandidate) -> dict[str, Any]:
        if candidate.candidate_id in self._local_candidate_ids:
            return self.fallback.get_recipe_detail(candidate)
        cached_detail = self._detail_cache.get(candidate.candidate_id)
        if cached_detail is not None:
            self._mode = "ai_generated"
            return deepcopy(cached_detail)
        self._mode = "ai_generated"
        raise AIRecipeProviderError("完整详情未在候选生成阶段准备成功，请重新搜索")

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


def _prepare_bundle_payload(
    payload: dict[str, Any],
    request: RecipeSearchRequest,
    limit: int,
) -> list[tuple[RecipeCandidate, dict[str, Any]]]:
    rows = payload.get("candidates")
    if not isinstance(rows, list) or not rows:
        raise AIRecipeProviderError("候选菜谱为空")
    prepared: list[tuple[RecipeCandidate, dict[str, Any]]] = []
    row_errors: list[Exception] = []
    for index, row in enumerate(rows[:limit], start=1):
        try:
            candidate = _candidate_from_row(row, index, request)
            raw = _detail_from_bundled_row(row, candidate, request)
        except (ValueError, TypeError) as exc:
            row_errors.append(exc)
            continue
        prepared.append((candidate, raw))
    if len(prepared) != limit:
        reason = str(row_errors[-1]) if row_errors else "候选数量不足"
        error = AIRecipeProviderError(
            f"需要{limit}个同时包含有效详情的候选菜谱：{reason}"
        )
        if row_errors:
            raise error from row_errors[-1]
        raise error
    candidates = [candidate for candidate, _ in prepared]
    if request.requested_dish and any(
        request.requested_dish not in candidate.title for candidate in candidates
    ):
        raise AIRecipeProviderError("候选没有全部保留用户指定菜名")
    return prepared


def _exception_chain_text(exc: BaseException) -> str:
    parts: list[str] = []
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        message = str(current).strip()
        if message:
            parts.append(message)
        current = current.__cause__
    return " <- ".join(parts)


def _is_correctable_recipe_error(exc: BaseException) -> bool:
    """Retry only malformed structure/details, not transport or hard safety constraints."""
    text = _exception_chain_text(exc)
    correctable_markers = (
        "候选菜谱为空",
        "候选必须是对象",
        "候选缺少完整recipe详情",
        "候选与完整菜谱名称不一致",
        "完整菜谱人数与请求不一致",
        "候选食材与完整菜谱不一致",
        "菜谱必须包含",
        "食材无效",
        "食材用量",
        "食材必须给出明确用量",
        "食材单位",
        "optional",
        "步骤说明无效",
        "步骤用量必须明确",
        "调料准备步骤",
        "候选数量不足",
    )
    return any(marker in text for marker in correctable_markers)


def _validation_issue(exc: BaseException) -> dict[str, str]:
    text = _exception_chain_text(exc)
    if isinstance(exc, QwenJSONOutputError):
        return {
            "code": "invalid_json",
            "path": "$",
            "message": "输出不是完整合法JSON，请重新生成完整对象",
        }
    code = "invalid_recipe"
    path = "candidates[0].recipe"
    if "步骤用量必须明确" in text:
        code = "vague_step_quantity"
        match = re.search(r"第(\d+)步", text)
        if match:
            path = f"candidates[0].recipe.steps[{int(match.group(1)) - 1}].instruction"
    elif "食材必须给出明确用量" in text or "食材用量" in text:
        code = "invalid_ingredient_amount"
        path = "candidates[0].recipe.ingredients"
    elif "optional" in text:
        code = "missing_ingredient_optional"
        path = "candidates[0].recipe.ingredients"
    elif "候选缺少完整recipe详情" in text:
        code = "missing_recipe"
        path = "candidates[0].recipe"
    elif "人数" in text:
        code = "invalid_servings"
        path = "candidates[0].recipe.servings"
    return {"code": code, "path": path, "message": text[:300]}


def _candidate_from_row(row: Any, index: int, request: RecipeSearchRequest) -> RecipeCandidate:
    if not isinstance(row, dict):
        raise ValueError("候选必须是对象")
    title = _text(row.get("title"), "菜名")
    summary = _text(row.get("summary"), "摘要")
    difficulty = str(row.get("difficulty") or "简单").strip()
    if difficulty not in {"简单", "中等"}:
        difficulty = "简单"
    minutes = validate_estimated_minutes(
        row.get("estimated_minutes"),
        maximum=request.max_cooking_minutes,
    )
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
    if request.requested_dish and request.requested_dish not in title:
        raise ValueError("候选没有保留指定菜名")
    if _candidate_was_excluded(title, request.excluded_candidate_ids):
        raise ValueError("候选与用户要求排除的菜谱重复")
    if not request.requested_dish and request.available_ingredients:
        labels_for_matching = [*ingredients, *inferred_seasonings]
        missing_inventory = [
            item for item in request.available_ingredients
            if not ingredient_present(item, labels_for_matching)
        ]
        if missing_inventory:
            raise ValueError(f"候选遗漏用户已有食材：{'、'.join(missing_inventory)}")
    candidate_id = f"ai_{_slug(title)}_{index}"
    return RecipeCandidate(
        candidate_id=candidate_id,
        title=title,
        source_name="千问 AI 生成",
        source_url=None,
        summary=summary,
        estimated_minutes=minutes,
        difficulty=difficulty,
        main_ingredients=ingredients,
        missing_ingredients=missing,
        match_reason=_text(row.get("match_reason"), "匹配说明"),
        main_seasonings=inferred_seasonings,
    )


def _detail_from_bundled_row(
    row: Any,
    candidate: RecipeCandidate,
    request: RecipeSearchRequest,
) -> dict[str, Any]:
    if not isinstance(row, dict) or not isinstance(row.get("recipe"), dict):
        raise ValueError("候选缺少完整recipe详情")
    raw = deepcopy(row["recipe"])
    _normalize_safe_model_variations(raw, candidate, request)
    if str(raw.get("title") or "").strip() != candidate.title:
        raise ValueError("候选与完整菜谱名称不一致")
    if request.servings is not None and raw.get("servings") != request.servings:
        raise ValueError("完整菜谱人数与请求不一致")
    _prepare_and_validate_recipe(raw, request)
    recipe_names = [
        str(item.get("name", ""))
        for item in raw.get("ingredients", [])
        if isinstance(item, dict)
    ]
    if any(
        not ingredient_present(label, recipe_names)
        for label in [*candidate.main_ingredients, *candidate.main_seasonings]
    ):
        raise ValueError("候选食材与完整菜谱不一致")
    raw["recipe_id"] = candidate.candidate_id
    raw["name"] = candidate.title
    raw.pop("title", None)
    raw["source_name"] = "千问 AI 生成"
    raw["source_url"] = None
    raw["notes"] = _string_list(raw.pop("safety_notes", []))
    return raw


def _normalize_safe_model_variations(
    raw: dict[str, Any],
    candidate: RecipeCandidate,
    request: RecipeSearchRequest,
) -> None:
    """Repair harmless omissions without inventing cooking instructions."""
    if not str(raw.get("title") or "").strip():
        raw["title"] = candidate.title
    if raw.get("servings") is None and request.servings is not None:
        raw["servings"] = request.servings
    elif isinstance(raw.get("servings"), str) and raw["servings"].strip().isdigit():
        raw["servings"] = int(raw["servings"].strip())
    if raw.get("estimated_minutes") is None:
        raw["estimated_minutes"] = candidate.estimated_minutes
    elif isinstance(raw.get("estimated_minutes"), float) and raw["estimated_minutes"].is_integer():
        raw["estimated_minutes"] = int(raw["estimated_minutes"])
    if raw.get("difficulty") is None:
        raw["difficulty"] = candidate.difficulty
    if raw.get("equipment") is None:
        raw["equipment"] = []
    if raw.get("safety_notes") is None:
        raw["safety_notes"] = []
    ingredients = raw.get("ingredients")
    if isinstance(ingredients, list):
        for item in ingredients:
            if isinstance(item, dict) and "optional" not in item:
                item["optional"] = False
        _fill_step_quantities_from_ingredients(raw, ingredients)


def _fill_step_quantities_from_ingredients(
    raw: dict[str, Any],
    ingredients: list[Any],
) -> None:
    """Replace vague step quantities with the recipe's declared quantities."""
    aliases: dict[str, tuple[str, str]] = {}
    ambiguous: set[str] = set()
    for item in ingredients:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        amount = str(item.get("amount") or "").strip()
        unit = str(item.get("unit") or "").strip()
        if not name or not amount or not unit:
            continue
        labels = {name}
        for prefix in ("食用", "白砂", "细砂"):
            if name.startswith(prefix) and len(name) > len(prefix):
                labels.add(name[len(prefix):])
        for group in INGREDIENT_GROUPS:
            if ingredient_matches(name, group):
                labels.add(group)
        if name == "清水":
            labels.add("水")
        if name.endswith("油") and len(name) > 1:
            labels.add("油")
        if name.endswith("粉") and name in {"椒盐粉", "白胡椒粉", "黑胡椒粉"}:
            labels.add(name[:-1])
        for label in labels:
            value = (name, f"{amount}{unit}")
            if label in aliases and aliases[label] != value:
                ambiguous.add(label)
            else:
                aliases[label] = value
    for label in ambiguous:
        aliases.pop(label, None)

    steps = raw.get("steps")
    if not isinstance(steps, list):
        return
    vague = r"(?:适量|少量|少许)"
    vague_action = (
        r"(?:加入|添加|放入|撒入|撒上|倒入|淋入|刷上|涂上|裹上|使用|用)"
    )
    for step in steps:
        if not isinstance(step, dict):
            continue
        instruction = str(step.get("instruction") or "")
        # “少量多次/少许分次”描述的是分批操作，不是总用量。
        instruction = re.sub(
            r"(?:少量|少许)(?:地)?(?:多次|分次)",
            "分批",
            instruction,
        )
        for label in sorted(aliases, key=len, reverse=True):
            canonical, quantity = aliases[label]
            escaped = re.escape(label)
            replacement = f"{quantity}{canonical}"
            instruction = re.sub(
                rf"{vague}(?:的)?(?P<action>{vague_action}){escaped}",
                lambda match: f"{match.group('action')}{replacement}",
                instruction,
            )
            instruction = re.sub(
                rf"{vague}(?:的)?{escaped}",
                replacement,
                instruction,
            )
            instruction = re.sub(
                rf"{escaped}(?:的)?(?:用量)?(?:为)?{vague}",
                replacement,
                instruction,
            )
            instruction = re.sub(
                rf"按口味(?:调整|{vague_action})?{escaped}",
                f"加入{replacement}",
                instruction,
            )
            instruction = re.sub(
                rf"{escaped}按口味(?:调整|增减|添加)?",
                replacement,
                instruction,
            )
        step["instruction"] = instruction


def _ensure_inventory_ingredients(raw: Any, request: RecipeSearchRequest) -> None:
    """Do not let a detail response silently drop pantry food from its candidate."""
    if request.requested_dish or not request.available_ingredients or not isinstance(raw, dict):
        return
    ingredient_rows = raw.get("ingredients")
    if not isinstance(ingredient_rows, list):
        return
    labels = [str(item.get("name", "")) for item in ingredient_rows if isinstance(item, dict)]
    missing = [item for item in request.available_ingredients if not ingredient_present(item, labels)]
    if missing:
        raise ValueError(f"完整菜谱遗漏用户已有食材：{'、'.join(missing)}")


def _ensure_equipment_constraints(raw: Any, request: RecipeSearchRequest) -> None:
    if not isinstance(raw, dict):
        return
    equipment = {str(item) for item in raw.get("equipment", []) if str(item)}
    if equipment & set(request.unavailable_equipment):
        raise ValueError("完整菜谱使用了用户没有的厨具")
    if request.equipment_only and request.available_equipment and equipment:
        if not equipment.issubset(set(request.available_equipment)):
            raise ValueError("完整菜谱超出用户现有厨具")


def _prepare_and_validate_recipe(raw: Any, request: RecipeSearchRequest) -> None:
    _ensure_profile_ingredients(raw, request)
    _ensure_inventory_ingredients(raw, request)
    _ensure_equipment_constraints(raw, request)
    _normalize_profile_timers(raw, request)
    ingredient_names = [
        str(item.get("name", ""))
        for item in raw.get("ingredients", [])
        if isinstance(item, dict)
    ] if isinstance(raw, dict) else []
    if ingredient_conflicts(ingredient_names, request.dietary_restrictions):
        raise ValueError("完整菜谱违反忌口")
    validate_raw_recipe(raw, request)


def _request_payload(request: RecipeSearchRequest) -> dict[str, Any]:
    return {
        "requested_dish": request.requested_dish,
        "available_ingredients": request.available_ingredients,
        "servings": request.servings,
        "taste_preferences": request.taste_preferences,
        "dietary_restrictions": request.dietary_restrictions,
        "max_cooking_minutes": request.max_cooking_minutes,
        "available_equipment": request.available_equipment,
        "unavailable_equipment": request.unavailable_equipment,
        "equipment_only": request.equipment_only,
        "difficulty_preference": request.difficulty_preference,
        "steak_doneness": request.steak_doneness,
        "steak_thickness_cm": request.steak_thickness_cm,
        "excluded_candidate_ids": request.excluded_candidate_ids,
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


def _slug(text: str) -> str:
    value = re.sub(r"[^\w\u4e00-\u9fff]+", "_", text).strip("_")
    return value[:40] or "recipe"


def _candidate_was_excluded(title: str, excluded_candidate_ids: list[str]) -> bool:
    slug = _slug(title)
    return any(candidate_id.startswith(f"ai_{slug}_") for candidate_id in excluded_candidate_ids)
