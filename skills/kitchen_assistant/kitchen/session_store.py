from __future__ import annotations

import threading
import time
from pathlib import Path
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
from .models import CookingContext, RecipeCandidate, RecipeSearchRequest
from .recipe_collection import collect_ingredients, collect_preferences, collect_request, start_session
from .recipe_confirmation import begin_cooking, confirm_meat_precondition, confirm_recipe
from .recipe_discovery import present_candidates
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
            "DoubaoClientError",
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
