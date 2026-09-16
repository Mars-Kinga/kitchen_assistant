from __future__ import annotations

import threading
import time
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

from .conversation_intents import is_affirmative, is_gratitude, is_step_acknowledgment
from .cooking_flow import (
    acknowledges_step_completion,
    advance_after_completion,
    answer_question,
    finished_feedback,
    handle_cooking_turn,
    handle_paused_turn,
    will_use_agent_for_question,
)
from .cooking_question_service import RuleBasedCookingQuestionService
from .cooking_timer_flow import (
    current_step_feedback,
    current_step_has_unfinished_timer,
    handle_parallel_step_confirmation,
    handle_parallel_timer_check,
    handle_timer,
    handle_timer_skip_confirmation,
    handle_unstarted_timer_confirmation,
    is_parallel_prep_ack,
    parallel_prep_candidate,
    parallel_prep_hint,
    request_early_timer_end,
    request_unstarted_timer_confirmation,
    start_step_timer,
    timer_completion_feedback,
    timer_end_if_due,
    timer_is_running_for_current_step,
    timer_still_running_response,
)
from .ingredient_vocabulary import canonicalize_ingredient
from .models import CookingContext, RecipeCandidate, RecipeSearchRequest
from .recipe_collection import collect_ingredients, collect_preferences, collect_request, start_session
from .recipe_confirmation import begin_cooking, confirm_meat_precondition, confirm_recipe, prepare_confirmed_recipe
from .recipe_discovery import present_candidates, search_recipes
from .recipe_normalizer import RecipeNormalizer
from .response_builder import feedback, result
from .response_phrases import GRATITUDE_RESPONSES, RandomPhrasePicker
from .session_presenter import public_metadata
from .states import (
    CANCELLED,
    COLLECTING_INGREDIENTS,
    COLLECTING_PREFERENCES,
    COLLECTING_REQUEST,
    COMPLETED,
    COOKING,
    IDLE,
    PAUSED,
    PRESENTING_CANDIDATES,
    WAITING_MEAT_THAW,
    WAITING_RECIPE_CONFIRMATION,
)
from .timer_controller import Timer, remaining_seconds


# 兼容第一版厨房助手的公开状态名。
COLLECTING_INFO = COLLECTING_PREFERENCES


class KitchenSessionBusyError(RuntimeError):
    status_code = 409


class KitchenSession:
    """厨房会话的状态和依赖容器。

    本文件只负责状态、分发和跨流程共享数据。需求收集、候选展示、菜谱
    确认、烹饪步骤、计时分别由对应功能模块实现；Provider/LLM 不能直接
    修改步骤或会话状态。
    """

    def __init__(
        self,
        recipe_path: Path | None = None,
        *,
        recipe_provider: Any | None = None,
        recipe_normalizer: RecipeNormalizer | None = None,
        cooking_question_service: Any | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if recipe_provider is None:
            from providers.mock_recipe_provider import MockRecipeSearchProvider

            recipes_dir = (
                recipe_path.parent
                if recipe_path
                else Path(__file__).resolve().parents[1] / "recipes"
            )
            recipe_provider = MockRecipeSearchProvider(recipes_dir)
        self.provider = recipe_provider
        self._active_provider = recipe_provider
        self._used_provider_fallback = False
        self.normalizer = recipe_normalizer or RecipeNormalizer()
        self.question_service = cooking_question_service or RuleBasedCookingQuestionService()
        self.offline_question_service = RuleBasedCookingQuestionService()
        self.clock = clock
        self.state = IDLE
        self.request = RecipeSearchRequest()
        self.recipe_candidates: list[RecipeCandidate] = []
        self.selected_candidate: RecipeCandidate | None = None
        self.current_recipe: dict[str, Any] | None = None
        self.step_index = 0
        self.timer: Timer | None = None
        self.conversation_summary: list[str] = []
        self.pending_step_confirmation: dict[str, Any] | None = None
        self.pending_timer_skip_confirmation = False
        self.pending_unstarted_timer_confirmation = False
        self.completed_parallel_step_indexes: set[int] = set()
        self.offered_parallel_step_indexes: set[int] = set()
        self.parallel_offer_by_timer_step: dict[int, int] = {}
        self.pending_parallel_step_index: int | None = None
        self.pending_parallel_timer_check_index: int | None = None
        self._timed_step_ready_for_completion: int | None = None
        self.phrases = RandomPhrasePicker()
        self._progress_callback: Callable[[dict[str, Any]], None] | None = None
        self._lock = threading.RLock()

    @property
    def recipe(self) -> dict[str, Any] | None:
        return self.current_recipe

    @property
    def servings(self) -> int | None:
        return self.request.servings

    @property
    def flavor(self) -> str | None:
        return self.request.taste_preferences[0] if self.request.taste_preferences else None

    def handle(self, user_text: str) -> dict[str, Any]:
        with self._lock:
            return self._handle(user_text)

    def set_progress_callback(
        self,
        callback: Callable[[dict[str, Any]], None] | None,
    ) -> None:
        self._progress_callback = callback if callable(callback) else None

    def _emit_progress(self, item: dict[str, Any]) -> bool:
        if self._progress_callback is None:
            return False
        try:
            self._progress_callback(item)
            return True
        except Exception:
            return False

    def _handle(self, user_text: str) -> dict[str, Any]:
        text = str(user_text or "").strip()
        if self._is_gratitude(text):
            active = self.state not in {IDLE, COMPLETED, CANCELLED}
            return self._result(
                self.state,
                active,
                feedback(
                    self.phrases.choose("thanks", GRATITUDE_RESPONSES),
                    "不用谢｜厨房助手随时在",
                    robot_action="nod",
                    led_effect="warm_white",
                    expression="happy",
                ),
                current_step=self.step_index + 1 if self.current_recipe else None,
            )
        if self.state in {IDLE, COMPLETED, CANCELLED}:
            return start_session(self, text)
        if (
            self._has(text, "退出", "取消", "不做了", "再见", "拜拜", "bye", "goodbye")
            and "取消计时" not in text
        ):
            self.state, self.timer = CANCELLED, None
            self.current_recipe = None
            self.selected_candidate = None
            self.step_index = 0
            self._timed_step_ready_for_completion = None
            self.pending_step_confirmation = None
            self.pending_timer_skip_confirmation = False
            self.pending_unstarted_timer_confirmation = False
            self.pending_parallel_step_index = None
            self.pending_parallel_timer_check_index = None
            self.completed_parallel_step_indexes.clear()
            self.offered_parallel_step_indexes.clear()
            self.parallel_offer_by_timer_step.clear()
            return self._result(
                CANCELLED,
                False,
                feedback(
                    "已结束本次厨房助手任务。需要时再叫我哦！",
                    "厨房助手已结束",
                    robot_action="wave_hand",
                    led_effect="warm_white",
                    expression="neutral",
                ),
            )
        if self.state in {COOKING, PAUSED}:
            timer_event = self._timer_end_if_due()
            if timer_event:
                return timer_event
            timer_response = self._handle_timer(text)
            if timer_response:
                return timer_response

        handlers = {
            COLLECTING_REQUEST: collect_request,
            COLLECTING_INGREDIENTS: collect_ingredients,
            COLLECTING_PREFERENCES: collect_preferences,
            PRESENTING_CANDIDATES: present_candidates,
            WAITING_RECIPE_CONFIRMATION: confirm_recipe,
            WAITING_MEAT_THAW: confirm_meat_precondition,
            PAUSED: handle_paused_turn,
        }
        handler = handlers.get(self.state)
        return handler(self, text) if handler else handle_cooking_turn(self, text)

    def poll(self) -> dict[str, Any] | None:
        with self._lock:
            if self.state != COOKING:
                return None
            return self._timer_end_if_due()

    def recommend_from_ingredients(
        self,
        ingredients: list[str],
        *,
        servings: int = 1,
        taste: str = "正常",
    ) -> dict[str, Any]:
        """Start one recommendation turn from a user-confirmed console pantry."""
        cleaned: list[str] = []
        for item in ingredients[:12]:
            name = canonicalize_ingredient(str(item))[:30]
            if name and name not in cleaned:
                cleaned.append(name)
        if not cleaned:
            raise ValueError("请至少确认一种食材。")
        if servings not in {1, 2, 3, 4, 5, 6}:
            raise ValueError("用餐人数必须在 1 到 6 人之间。")
        if taste not in {"正常", "少盐", "辣"}:
            raise ValueError("口味只能是正常、少盐或辣。")
        with self._lock:
            self._reset()
            self.request.available_ingredients = cleaned
            self.request.servings = servings
            self.request.taste_preferences = [taste]
            self.conversation_summary.append(f"控制台确认食材：{'、'.join(cleaned)}")
            return search_recipes(self)

    def generate_console_recipe(self, dish_name: str, *, servings: int = 2) -> dict[str, Any]:
        """Generate a fresh named recipe after explicit LAN-console confirmation."""
        dish = str(dish_name or "").strip()[:60]
        if not dish:
            raise ValueError("请输入要生成的菜名。")
        if servings not in {1, 2, 3, 4, 5, 6}:
            raise ValueError("用餐人数必须在 1 到 6 人之间。")
        if not bool(getattr(self.provider, "supports_ai", False)):
            raise RuntimeError("当前没有配置可用的 AI 菜谱生成服务。")
        with self._lock:
            self._reset()
            self.request = RecipeSearchRequest(
                requested_dish=dish,
                servings=servings,
                taste_preferences=["正常"],
                bypass_cache=True,
            )
            self.conversation_summary.append(f"控制台确认生成菜谱：{dish}")
            return search_recipes(self)

    def status_snapshot(self) -> dict[str, Any]:
        """Return a JSON-safe read model for the LAN console."""
        with self._lock:
            recipe = self.current_recipe or {}
            steps = recipe.get("steps") if isinstance(recipe.get("steps"), list) else []
            current_step: dict[str, Any] | None = None
            if steps and 0 <= self.step_index < len(steps):
                step = steps[self.step_index]
                current_step = {
                    "number": self.step_index + 1,
                    "total": len(steps),
                    "instruction": str(step.get("instruction") or ""),
                    "duration_seconds": step.get("duration_seconds"),
                    "heat_level": step.get("heat_level"),
                    "safety_note": step.get("safety_note"),
                }
            timer: dict[str, Any] | None = None
            if self.timer is not None:
                remaining = self._remaining_seconds()
                timer = {
                    "label": self.timer.label,
                    "remaining_seconds": remaining,
                    "duration_seconds": self.timer.seconds,
                    "state": (
                        "paused"
                        if self.timer.paused_remaining_seconds is not None
                        else ("due" if remaining == 0 else "running")
                    ),
                }
            parallel_step: dict[str, Any] | None = None
            if self.timer is not None and self.timer.step_index is not None:
                parallel_index = self.parallel_offer_by_timer_step.get(self.timer.step_index)
                if parallel_index is not None and 0 <= parallel_index < len(steps):
                    parallel_step = {
                        "number": parallel_index + 1,
                        "instruction": str(steps[parallel_index].get("instruction") or ""),
                        "completed": parallel_index in self.completed_parallel_step_indexes,
                    }
            return {
                "state": self.state,
                "session_active": self.state not in {IDLE, COMPLETED, CANCELLED},
                "recipe": {
                    "recipe_id": recipe.get("recipe_id"),
                    "name": recipe.get("name"),
                    "estimated_minutes": recipe.get("estimated_time_minutes"),
                    "difficulty": recipe.get("difficulty"),
                    **({"import_metadata": recipe["import_metadata"]} if recipe.get("import_metadata") else {}),
                } if recipe else None,
                "current_step": current_step,
                "parallel_step": parallel_step,
                "timer": timer,
                "candidates": [candidate.as_dict() for candidate in self.recipe_candidates],
                "confirmed_ingredients": list(self.request.available_ingredients),
                "provider_mode": self._provider_mode(),
            }

    def browse_local_recipes(self, query: str = "", *, category: str = "", limit: int = 500) -> list[dict[str, Any]]:
        provider = self._local_catalog_provider()
        browse = getattr(provider, "list_recipes", None)
        if not callable(browse):
            return []
        return browse(query, category=category, limit=limit)

    def browse_recipe_categories(self) -> list[dict[str, Any]]:
        provider = self._local_catalog_provider()
        list_categories = getattr(provider, "list_categories", None)
        return list_categories() if callable(list_categories) else []

    def console_recipe_detail(self, recipe_id: str, *, servings: int = 2) -> dict[str, Any]:
        with self._lock:
            if self.current_recipe and str(self.current_recipe.get("recipe_id")) == str(recipe_id):
                return dict(self.current_recipe)
            candidate = next(
                (item for item in self.recipe_candidates if item.candidate_id == str(recipe_id)),
                None,
            )
            if candidate is not None:
                raw = self._active_provider.get_recipe_detail(candidate)
            else:
                provider = self._local_catalog_provider()
                raw = provider.get_recipe_by_id(str(recipe_id))
            return self.normalizer.normalize(raw, servings=servings)

    def select_console_recipe(self, recipe_id: str, *, servings: int = 2) -> dict[str, Any]:
        with self._lock:
            active_candidate = next(
                (item for item in self.recipe_candidates if item.candidate_id == str(recipe_id)),
                None,
            )
            if active_candidate is not None:
                self.request.servings = servings
                self.selected_candidate = active_candidate
                self.state = WAITING_RECIPE_CONFIRMATION
                return confirm_recipe(self, "开始")

            provider = self._local_catalog_provider()
            raw = provider.get_recipe_by_id(str(recipe_id))
            request = RecipeSearchRequest(
                requested_dish=str(raw.get("name") or ""),
                servings=servings,
                taste_preferences=["正常"],
            )
            candidates = provider.search_recipes(request)
            candidate = next((item for item in candidates if item.candidate_id == str(recipe_id)), None)
            if candidate is None:
                raise ValueError("无法选择这道本地菜谱。")
            self._reset()
            self.request = request
            self._active_provider = provider
            self.recipe_candidates = [candidate]
            self.selected_candidate = candidate
            self.state = WAITING_RECIPE_CONFIRMATION
            return confirm_recipe(self, "开始")

    def select_imported_console_recipe(self, recipe: dict[str, Any]) -> dict[str, Any]:
        """Start the reviewed version without normalizing its workflow again."""
        with self._lock:
            if self.state not in {IDLE, COMPLETED, CANCELLED}:
                raise KitchenSessionBusyError("请先结束当前厨房任务，再开始这份视频菜谱。")
            metadata = recipe.get("import_metadata")
            if not isinstance(metadata, dict) or metadata.get("confirmed") is not True or not recipe.get("steps"):
                raise ValueError("导入菜谱尚未确认或没有完整步骤。")
            self._reset()
            self.current_recipe = deepcopy(recipe)
            self.request = RecipeSearchRequest(
                requested_dish=recipe["name"], servings=recipe["servings"],
                taste_preferences=["正常"],
            )
            self._active_provider = SimpleNamespace(mode="local_cache")
            self.conversation_summary.append(f"已确认的视频菜谱：{recipe['name']}")
            return prepare_confirmed_recipe(self)

    def navigate_console_step(self, direction: str) -> dict[str, Any]:
        """Move steps and return the same robot feedback used by conversation."""
        if direction not in {"previous", "next"}:
            raise ValueError("direction 只能是 previous 或 next。")
        with self._lock:
            steps = self.current_recipe.get("steps", []) if self.current_recipe else []
            if not steps:
                raise ValueError("当前没有正在进行的菜谱。")
            if self.state == COMPLETED:
                raise ValueError("这道菜已经完成，可以继续选择其他菜谱。")
            if self.state == WAITING_MEAT_THAW:
                raise ValueError("请先确认肉类或水产是新鲜食材或已完全解冻，再开始第1步。")
            if self.state == PAUSED:
                raise ValueError("请先恢复烹饪指导，再切换步骤。")
            if self.state != COOKING:
                raise ValueError("当前没有正在进行的烹饪任务，请重新选择菜谱。")
            if self.current_recipe.get("import_metadata"):
                if direction == "next":
                    return self._handle("下一步")
                if self.timer is not None:
                    raise ValueError("当前计时尚未结束，请先完成当前步骤或取消计时。")
            old_index = self.step_index
            if direction == "next" and old_index >= len(steps) - 1:
                self.timer = None
                self.pending_step_confirmation = None
                self.pending_timer_skip_confirmation = False
                self.pending_unstarted_timer_confirmation = False
                return self._advance_after_completion()
            if direction == "previous":
                self.step_index = max(0, self.step_index - 1)
            else:
                self.step_index = min(len(steps) - 1, self.step_index + 1)
            if self.timer is not None and self.timer.step_index == old_index and self.step_index != old_index:
                self.timer = None
            self.pending_step_confirmation = None
            self.pending_timer_skip_confirmation = False
            self.pending_unstarted_timer_confirmation = False
            timer_feedback = None
            if self.step_index != old_index and not self.current_recipe.get("import_metadata"):
                timer_feedback = self._start_step_timer()
            prefix = "返回：" if direction == "previous" else "下一步："
            step_feedback = self._current_step_feedback(prefix)
            items = [step_feedback]
            if timer_feedback is not None:
                items.append(timer_feedback)
            return self._result(
                self.state,
                True,
                *items,
                current_step=self.step_index + 1,
            )

    def console_cooking_action(self, action: str) -> dict[str, Any]:
        actions = {
            "start_timer": "开始计时", "confirm_done": "确认完成", "pause": "暂停",
            "resume": "恢复", "cancel_task": "取消", "cancel_timer": "取消计时",
            "fresh_ingredients": "新鲜食材",
        }
        if action not in actions:
            raise ValueError("未知烹饪操作。")
        with self._lock:
            if self.state in {IDLE, COMPLETED, CANCELLED}:
                raise ValueError("当前没有活动厨房任务。")
            if action == "fresh_ingredients" and self.state != WAITING_MEAT_THAW:
                raise ValueError("当前不需要确认解冻状态。")
            if action not in {"cancel_task", "fresh_ingredients"} and self.state not in {COOKING, PAUSED}:
                raise ValueError("请先完成菜谱和食材状态确认。")
            text = "确认" if action == "confirm_done" and self.pending_timer_skip_confirmation else actions[action]
            return self._handle(text)

    def _local_catalog_provider(self) -> Any:
        provider = self.provider
        fallback = getattr(provider, "fallback", None)
        if callable(getattr(provider, "list_recipes", None)):
            return provider
        if fallback is not None and callable(getattr(fallback, "list_recipes", None)):
            return fallback
        raise RuntimeError("本地菜谱目录不可用。")

    # 以下薄封装保留旧版内部调用/测试兼容，同时把实现明确路由到功能模块。
    def _confirm_recipe(self, text: str) -> dict[str, Any]:
        return confirm_recipe(self, text)

    def _confirm_meat_thaw(self, text: str) -> dict[str, Any]:
        return confirm_meat_precondition(self, text)

    def _begin_cooking(self) -> dict[str, Any]:
        return begin_cooking(self)

    def _cook(self, text: str) -> dict[str, Any]:
        return handle_cooking_turn(self, text)

    def _paused(self, text: str) -> dict[str, Any]:
        return handle_paused_turn(self, text)

    def _answer_question(self, text: str) -> dict[str, Any] | None:
        return answer_question(self, text)

    def _will_use_agent_for_question(self, text: str, context: CookingContext) -> bool:
        return will_use_agent_for_question(self, text, context)

    def _handle_timer(self, text: str) -> dict[str, Any] | None:
        return handle_timer(self, text)

    def _timer_end_if_due(self) -> dict[str, Any] | None:
        return timer_end_if_due(self)

    def _timer_completion_feedback(self, timer: Timer) -> dict[str, str]:
        return timer_completion_feedback(self, timer)

    def _start_step_timer(self, label: str | None = None) -> dict[str, str] | None:
        return start_step_timer(self, label)

    def _request_early_timer_end(self) -> dict[str, Any]:
        return request_early_timer_end(self)

    def _handle_timer_skip_confirmation(self, text: str) -> dict[str, Any]:
        return handle_timer_skip_confirmation(self, text)

    def _current_step_has_unfinished_timer(self) -> bool:
        return current_step_has_unfinished_timer(self)

    def _request_unstarted_timer_confirmation(self) -> dict[str, Any]:
        return request_unstarted_timer_confirmation(self)

    def _handle_unstarted_timer_confirmation(self, text: str) -> dict[str, Any]:
        return handle_unstarted_timer_confirmation(self, text)

    def _handle_parallel_step_confirmation(self, text: str) -> dict[str, Any]:
        return handle_parallel_step_confirmation(self, text)

    def _handle_parallel_timer_check(self, text: str) -> dict[str, Any]:
        return handle_parallel_timer_check(self, text)

    def _timer_still_running_response(self) -> dict[str, Any]:
        return timer_still_running_response(self)

    def _current_step_feedback(self, prefix: str = "") -> dict[str, str]:
        return current_step_feedback(self, prefix)

    def _parallel_prep_candidate(self) -> tuple[int, str] | None:
        return parallel_prep_candidate(self)

    def _parallel_prep_hint(
        self,
        candidate: tuple[int, str] | None = None,
    ) -> str | None:
        return parallel_prep_hint(self, candidate)

    def _timer_is_running_for_current_step(self) -> bool:
        return timer_is_running_for_current_step(self)

    def _is_parallel_prep_ack(self, text: str) -> bool:
        return is_parallel_prep_ack(self, text)

    @staticmethod
    def _acknowledges_step_completion(text: str) -> bool:
        return acknowledges_step_completion(text)

    def _advance_after_completion(self) -> dict[str, Any]:
        return advance_after_completion(self)

    def _finished_feedback(self) -> dict[str, str]:
        return finished_feedback(self)

    def _step(self) -> dict[str, Any]:
        assert self.current_recipe is not None
        return self.current_recipe["steps"][self.step_index]

    def _ask_ingredients(self) -> dict[str, str]:
        return feedback(
            "你现在有哪些食材？可以直接说鸡蛋、番茄、面条之类的。",
            "请说出已有食材，例如：鸡蛋、番茄、面条",
            robot_action="nod",
            led_effect="blue",
            expression="curious",
            question=True,
        )

    def _provider_mode(self) -> str:
        return str(getattr(self._active_provider, "mode", "mock"))

    def _remaining_seconds(self) -> int | None:
        return remaining_seconds(self.timer, self.clock())

    def _result(
        self,
        state: str,
        active: bool,
        *items: dict[str, Any],
        **metadata: Any,
    ) -> dict[str, Any]:
        metadata.setdefault("provider_mode", self._provider_mode())
        return public_metadata(result(state, active, *items, **metadata))

    def _with_prefix(self, prefix: dict[str, Any], response: dict[str, Any]) -> dict[str, Any]:
        if "steps" in response:
            response["steps"] = [prefix, *response["steps"]]
        else:
            keys = ("speech", "question", "display", "robot_action", "led_effect", "expression")
            primary = {key: response.pop(key) for key in keys if key in response}
            response["steps"] = [prefix, primary]
        return response

    def _reset(self) -> None:
        self.state = IDLE
        self.request = RecipeSearchRequest()
        self.recipe_candidates = []
        self.selected_candidate = None
        self.current_recipe = None
        self.step_index = 0
        self.timer = None
        self._timed_step_ready_for_completion = None
        self.conversation_summary = []
        self.pending_step_confirmation = None
        self.pending_timer_skip_confirmation = False
        self.pending_unstarted_timer_confirmation = False
        self.completed_parallel_step_indexes = set()
        self.offered_parallel_step_indexes = set()
        self.parallel_offer_by_timer_step = {}
        self.pending_parallel_step_index = None
        self.pending_parallel_timer_check_index = None
        self._active_provider = self.provider
        self._used_provider_fallback = False

    @staticmethod
    def _has(text: str, *phrases: str) -> bool:
        return any(phrase in text for phrase in phrases)

    def _new_cache_filename(self) -> str | None:
        path = getattr(self._active_provider, "last_cache_path", None)
        return path.name if isinstance(path, Path) else None

    def _cache_candidate_count(self) -> int:
        count = getattr(self._active_provider, "last_cache_candidate_count", 0)
        return int(count) if isinstance(count, int) else 0

    @staticmethod
    def _is_affirmative(text: str) -> bool:
        return is_affirmative(text)

    @staticmethod
    def _safe_detail_error(exc: Exception) -> str:
        """保留错误类型链，但不输出请求、密钥或服务端响应正文。"""
        safe_names = {
            "AIRecipeProviderError",
            "QwenClientError",
            "RecipeNormalizationError",
            "ValueError",
            "TypeError",
        }
        parts: list[str] = []
        current: BaseException | None = exc
        while current is not None and len(parts) < 4:
            name = type(current).__name__
            message = str(current).strip() if name in safe_names else ""
            item = f"{name}: {message}" if message else name
            if item not in parts:
                parts.append(item)
            current = current.__cause__
        return " <- ".join(parts)

    @staticmethod
    def _is_gratitude(text: str) -> bool:
        return is_gratitude(text)

    @staticmethod
    def _is_step_acknowledgment(text: str) -> bool:
        return is_step_acknowledgment(text)
