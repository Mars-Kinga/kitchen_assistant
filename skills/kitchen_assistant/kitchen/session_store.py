from __future__ import annotations

import time
import threading
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .cooking_question_service import RuleBasedCookingQuestionService, STOP_AND_CHECK
from .dietary_rules import ingredient_conflicts
from .food_safety import RAW_MEAT_TERMS
from .dish_profiles import profile_questions
from .intent_parser import (
    extract_serving_choice,
    extract_timer_seconds,
    is_likely_step_completion,
    is_likely_next_step,
    is_likely_timer_start,
)
from .models import CookingContext, RecipeCandidate, RecipeSearchRequest
from .recipe_normalizer import RecipeNormalizer
from .request_parser import apply_updates, parse_updates, select_candidate
from .response_builder import feedback, result
from .response_phrases import (
    FINISHED_RESPONSES, GRATITUDE_RESPONSES, SINGLE_DINER_COMPANIONS,
    STEP_ENCOURAGEMENTS, WAITING_ACKNOWLEDGMENTS, RandomPhrasePicker,
)
from .states import (
    CANCELLED, COLLECTING_INGREDIENTS, COLLECTING_PREFERENCES, COLLECTING_REQUEST,
    COMPLETED, COOKING, IDLE, PAUSED, PRESENTING_CANDIDATES, SEARCHING_RECIPES,
    WAITING_MEAT_THAW, WAITING_RECIPE_CONFIRMATION,
)

# Kept for callers written against the first kitchen MVP.
COLLECTING_INFO = COLLECTING_PREFERENCES

FREE_WAIT_DURING_TIMER_HINT = "在这段时间里你可以同步做自己想做的事情，时间到了我会叫你～"


@dataclass
class Timer:
    deadline: float
    seconds: int
    label: str = "烹饪"
    step_index: int | None = None
    paused_remaining_seconds: int | None = None


class KitchenSession:
    """Deterministic, single-process kitchen session with provider boundaries.

    Search, normalization and cooking Q&A are injected services. The state
    machine remains local, so neither an Agent nor an LLM can alter steps.
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

            recipes_dir = (recipe_path.parent if recipe_path else Path(__file__).resolve().parents[1] / "recipes")
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

    # Compatibility aliases for the initial MVP and simple introspection.
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

    def set_progress_callback(self, callback: Callable[[dict[str, Any]], None] | None) -> None:
        """Set a turn-scoped callback for immediate waiting-state feedback."""
        self._progress_callback = callback if callable(callback) else None

    def _emit_progress(self, item: dict[str, Any]) -> bool:
        if self._progress_callback is None:
            return False
        try:
            self._progress_callback(item)
            return True
        except Exception:
            # A display issue must never cancel an AI request or cooking turn.
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
                    robot_action="nod", led_effect="warm_white", expression="happy",
                ),
                current_step=self.step_index + 1 if self.current_recipe else None,
            )
        if self.state in {IDLE, COMPLETED, CANCELLED}:
            return self._start(text)

        if self._has(text, "退出", "取消", "不做了", "再见", "拜拜", "bye", "goodbye") and "取消计时" not in text:
            self.state, self.timer = CANCELLED, None
            return self._result(CANCELLED, False, feedback("已结束本次厨房助手任务。需要时再叫我哦！", "厨房助手已结束", robot_action="wave_hand", led_effect="warm_white", expression="neutral"))

        if self.state in {COOKING, PAUSED}:
            timer_event = self._timer_end_if_due()
            if timer_event:
                return timer_event
            timer_response = self._handle_timer(text)
            if timer_response:
                return timer_response

        if self.state == COLLECTING_REQUEST:
            return self._collect_request(text)
        if self.state == COLLECTING_INGREDIENTS:
            return self._collect_ingredients(text)
        if self.state == COLLECTING_PREFERENCES:
            return self._collect_preferences(text)
        if self.state == PRESENTING_CANDIDATES:
            return self._presenting_candidates(text)
        if self.state == WAITING_RECIPE_CONFIRMATION:
            return self._confirm_recipe(text)
        if self.state == WAITING_MEAT_THAW:
            return self._confirm_meat_thaw(text)
        if self.state == PAUSED:
            return self._paused(text)
        return self._cook(text)

    def poll(self) -> dict[str, Any] | None:
        """Return an asynchronous timer event without consuming user input."""
        with self._lock:
            if self.state not in {COOKING, PAUSED} or self.state == PAUSED:
                return None
            return self._timer_end_if_due()

    def _start(self, text: str) -> dict[str, Any]:
        self._reset()
        updates = parse_updates(text)
        apply_updates(self.request, updates)
        welcome = feedback("欢迎使用 AI 厨房助手。", "AI 厨房助手", robot_action="wave_hand", led_effect="blue_dynamic", expression="happy")
        if self.request.requested_dish:
            self.state = COLLECTING_PREFERENCES
            return self._with_prefix(welcome, self._next_preference_response())
        if updates.asks_for_recommendation or self.request.available_ingredients:
            self.state = COLLECTING_INGREDIENTS if not self.request.available_ingredients else COLLECTING_PREFERENCES
            if self.state == COLLECTING_INGREDIENTS:
                return self._with_prefix(welcome, self._result(COLLECTING_INGREDIENTS, True, self._ask_ingredients()))
            return self._with_prefix(welcome, self._next_preference_response())
        self.state = COLLECTING_REQUEST
        return self._with_prefix(welcome, self._result(COLLECTING_REQUEST, True, feedback("你已经想好要做什么了，还是想根据现有食材推荐呢？", "选择方式：指定菜名 / 根据食材推荐", robot_action="nod", led_effect="blue", expression="curious", question=True)))

    def _collect_request(self, text: str) -> dict[str, Any]:
        updates = parse_updates(text)
        apply_updates(self.request, updates)
        if self.request.requested_dish:
            self.state = COLLECTING_PREFERENCES
            return self._next_preference_response()
        if updates.asks_for_recommendation or self.request.available_ingredients:
            self.state = COLLECTING_INGREDIENTS
            return self._collect_ingredients(text)
        return self._result(COLLECTING_REQUEST, True, feedback("你可以直接说想做的菜名，或者告诉我冰箱里现有的食材，我来帮你想菜谱哦。", "请说菜名，或说：我有鸡蛋、番茄和面条", robot_action="nod", led_effect="blue", expression="curious", question=True))

    def _collect_ingredients(self, text: str) -> dict[str, Any]:
        apply_updates(self.request, parse_updates(text))
        if not self.request.available_ingredients:
            return self._result(COLLECTING_INGREDIENTS, True, self._ask_ingredients())
        self.state = COLLECTING_PREFERENCES
        return self._next_preference_response()

    def _collect_preferences(self, text: str) -> dict[str, Any]:
        previous_servings = self.request.servings
        updates = parse_updates(text)
        # A bare “1” has meaning only while the assistant is explicitly
        # collecting servings; elsewhere it should not change the request.
        if self.request.servings is None and updates.servings is None:
            updates.servings = extract_serving_choice(text)
        apply_updates(self.request, updates)
        response = self._next_preference_response()
        if previous_servings is None and self.request.servings == 1:
            companion = self.phrases.choose("single_diner", SINGLE_DINER_COMPANIONS)
            if "question" in response:
                response["question"] = f"{companion}{response['question']}"
                response["display"] = f"一个人的小厨房，也有陪伴\n{response['display']}"
                response["robot_action"] = "encourage_gesture"
                response["led_effect"] = "warm_white"
                response["expression"] = "happy"
        return response

    def _next_preference_response(self) -> dict[str, Any]:
        if self.request.servings is None:
            return self._result(COLLECTING_PREFERENCES, True, feedback("请问是几个人吃？", "请选择人数：1 人 / 2 人 / 3 人", robot_action="nod", led_effect="blue", expression="curious", question=True))
        for question in profile_questions(self.request.requested_dish):
            field = str(question.get("field", ""))
            if field and getattr(self.request, field, None) is None:
                return self._result(COLLECTING_PREFERENCES, True, feedback(
                    str(question.get("question", "请补充菜谱所需信息。")),
                    str(question.get("display", "请补充菜谱所需信息")),
                    robot_action="nod", led_effect="blue", expression="curious", question=True,
                ))
        if not self.request.taste_preferences:
            return self._result(COLLECTING_PREFERENCES, True, feedback("想要正常口味、少盐清淡，还是想吃辣？", "请选择口味：正常 / 少盐 / 辣", robot_action="nod", led_effect="blue", expression="curious", question=True))
        return self._search_recipes()

    def _search_recipes(self) -> dict[str, Any]:
        self.state = SEARCHING_RECIPES
        self._active_provider = self.provider
        self._used_provider_fallback = False
        configured_ai = bool(getattr(self.provider, "supports_ai", False)) or self._provider_mode() == "ai_generated"
        progress_emitted = False
        if configured_ai:
            progress_emitted = self._emit_progress(feedback(
                "请稍后，正在为你查找菜谱。",
                "请稍后，正在为你查找菜谱",
                robot_action="turn_left", led_effect="blue_dynamic", expression="focused",
            ))
        try:
            self.recipe_candidates = self._active_provider.search_recipes(self.request)
        except Exception:
            # An explicit fresh-search request must never silently fall back
            # to the exact cache the user asked us to bypass.
            if self.request.bypass_cache:
                self.recipe_candidates = []
            else:
                fallback = getattr(self.provider, "fallback", None)
                if fallback is None:
                    self.recipe_candidates = []
                else:
                    self._active_provider = fallback
                    self._used_provider_fallback = True
                    self.recipe_candidates = fallback.search_recipes(self.request)
                    # A local fallback must not silently turn a specifically
                    # requested dish into a recipe that merely shares one
                    # ingredient. Keep only exact-name variants in this case.
                    if configured_ai and self.request.requested_dish:
                        self.recipe_candidates = [
                            candidate for candidate in self.recipe_candidates
                            if self.request.requested_dish in candidate.title
                        ]
        self.state = PRESENTING_CANDIDATES
        actual_mode = self._provider_mode()
        if actual_mode == "local_cache":
            searching = feedback(
                "我找到了几个菜谱。",
                "已找到菜谱",
                robot_action="nod", led_effect="green_dynamic", expression="happy",
            )
        elif actual_mode == "ai_generated":
            searching = feedback(
                "我正在根据你的需求生成几个适合的菜谱建议，请稍等哦。",
                "我正在为你生成菜谱建议……",
                robot_action="turn_left", led_effect="blue_dynamic", expression="focused",
            )
        else:
            searching = feedback(
                "我正在查找一些适合你的菜谱，请稍等哦。",
                "正在搜索美味菜谱……",
                robot_action="turn_left", led_effect="blue_dynamic", expression="focused",
            )
        if not self.recipe_candidates:
            if self.request.bypass_cache:
                speech = "联网生成服务暂时不可用，我没有退回本地缓存。请检查联网生成服务配置后再试。"
            else:
                speech = (
                    "没有找到与指定菜名匹配的离线菜谱，我不会用无关菜谱替代。请配置生成服务，或换个菜名。"
                    if self.request.requested_dish
                    else "没有找到能同时使用你已说明食材的离线菜谱，我不会忽略其中任何一种。请配置生成服务，或补充食材后再试。"
                )
            return self._result(PRESENTING_CANDIDATES, True, searching, feedback(speech, "暂无候选｜没有匹配菜谱", robot_action="nod", led_effect="white", expression="neutral"), recipe_candidates=[], provider_mode=actual_mode)
        candidate_feedback = feedback(
            "我列出了几个建议，想吃哪个呢？你可以说第一个、第二个或者直接说菜名。" if actual_mode == "ai_generated" else "我找到了几个菜谱。你可以说第一个、第二个或者直接说菜名。",
            self._candidate_display(), robot_action="nod", led_effect="green_dynamic", expression="happy",
        )
        items = [] if progress_emitted else [searching]
        if self._used_provider_fallback:
            fallback_speech = (
                "生成服务暂时不可用，我先使用本地菜谱为你推荐。"
                if configured_ai else "目前没有配置真实联网服务，我先使用本地菜谱为你推荐。"
            )
            items.append(feedback(fallback_speech, "已切换离线菜谱模式", robot_action="nod", led_effect="warm_white", expression="neutral"))
        items.append(candidate_feedback)
        return self._result(PRESENTING_CANDIDATES, True, *items, recipe_candidates=[candidate.as_dict() for candidate in self.recipe_candidates], provider_mode=self._provider_mode())

    def _presenting_candidates(self, text: str) -> dict[str, Any]:
        updates = parse_updates(text)
        if updates.bypass_cache:
            if not getattr(self.provider, "supports_ai", False):
                return self._result(
                    PRESENTING_CANDIDATES,
                    True,
                    feedback(
                        "当前没有配置可用的联网生成服务，我不会假装已经完成上网搜索，也不会改用本地缓存。",
                        "联网搜索不可用｜保留当前候选",
                        robot_action="show_concern", led_effect="yellow", expression="alert", question=True,
                    ),
                    recipe_candidates=[candidate.as_dict() for candidate in self.recipe_candidates],
                    provider_mode=self._provider_mode(),
                )
            apply_updates(self.request, updates)
            self.request.excluded_candidate_ids = []
            self.selected_candidate = None
            return self._search_recipes()
        if self._has(text, "换一批", "换一个", "不要这个"):
            self.request.excluded_candidate_ids.extend(candidate.candidate_id for candidate in self.recipe_candidates if candidate.candidate_id not in self.request.excluded_candidate_ids)
            return self._search_recipes()
        if self._has(text, "更简单", "简单一点", "更快", "快一点"):
            apply_updates(self.request, parse_updates(text))
            self.request.excluded_candidate_ids = []
            return self._search_recipes()
        if self.recipe_candidates and self._has(text, "就这个", "按这个"):
            self.selected_candidate = self.recipe_candidates[0]
            self.state = WAITING_RECIPE_CONFIRMATION
            overview = self._selected_summary()
            if self._has(text, "可以", "开始", "确认"):
                cooking = self._confirm_recipe("开始吧")
                overview_feedback = {key: overview[key] for key in ("speech", "question", "display", "robot_action", "led_effect", "expression") if key in overview}
                if "steps" in cooking:
                    cooking["steps"].insert(0, overview_feedback)
                else:
                    cooking["steps"] = [overview_feedback, {key: cooking.pop(key) for key in ("speech", "question", "display", "robot_action", "led_effect", "expression") if key in cooking}]
                return cooking
            return overview
        choice = select_candidate(text, [candidate.title for candidate in self.recipe_candidates])
        if choice is not None:
            self.selected_candidate = self.recipe_candidates[choice]
            self.state = WAITING_RECIPE_CONFIRMATION
            return self._selected_summary()
        return self._result(PRESENTING_CANDIDATES, True, feedback("请说第一个、第二个、菜名，或说换一批。", "请选择候选：第一个 / 第二个 / 菜名 / 换一批", robot_action="nod", led_effect="green_dynamic", expression="curious", question=True), recipe_candidates=[candidate.as_dict() for candidate in self.recipe_candidates], provider_mode=self._provider_mode())

    def _selected_summary(self) -> dict[str, Any]:
        assert self.selected_candidate is not None
        candidate = self.selected_candidate
        missing = "、".join(candidate.missing_ingredients)
        generated = self._provider_mode() == "ai_generated"
        cached = self._provider_mode() == "local_cache"
        source = "生成方式：根据你的需求生成" if generated else ("来源：本地已保存菜谱" if cached else f"来源：{candidate.source_name}")
        inventory = (
            f"缺少：{missing or '无'}"
            if self.request.available_ingredients
            else "食材库存：未提供，确认后给你完整用量清单"
        )
        seasonings = "、".join(candidate.main_seasonings) or "未单列"
        display = f"{candidate.title}\n{source}\n{candidate.estimated_minutes or '未知'} 分钟｜{candidate.difficulty}\n主要食材：{'、'.join(candidate.main_ingredients)}\n主要调味料：{seasonings}\n{inventory}"
        question = "这是我根据你的需求生成的菜谱，要按照这个开始吗？" if generated else f"已选{candidate.title}，来源是{candidate.source_name}，预计{candidate.estimated_minutes or '未知'}分钟，难度{candidate.difficulty}。要按照这个菜谱开始吗？"
        return self._result(WAITING_RECIPE_CONFIRMATION, True, feedback(question, display, robot_action="nod", led_effect="green", expression="confident", question=True), selected_candidate=candidate.as_dict(), provider_mode=self._provider_mode())

    def _confirm_recipe(self, text: str) -> dict[str, Any]:
        if self._has(text, "更简单", "简单一点", "更快", "快一点"):
            apply_updates(self.request, parse_updates(text))
            self.request.excluded_candidate_ids = []
            return self._search_recipes()
        if self._has(text, "换", "不要"):
            self.state = PRESENTING_CANDIDATES
            return self._presenting_candidates("换一批" if "换一批" in text else "")
        if not self._is_affirmative(text):
            return self._result(WAITING_RECIPE_CONFIRMATION, True, feedback("请明确说可以、开始吧、就这个或按这个做；也可以说换一个。", "等待确认：开始 / 换一个", robot_action="nod", led_effect="green", expression="confident", question=True), selected_candidate=self.selected_candidate.as_dict() if self.selected_candidate else None, provider_mode=self._provider_mode())
        assert self.selected_candidate is not None
        try:
            self.current_recipe = self.normalizer.normalize(
                self._active_provider.get_recipe_detail(self.selected_candidate),
                servings=self.request.servings,
            )
        except Exception:
            # Never replace the dish the user selected with an unrelated local
            # fallback. Keep the candidate so “重试” can request its detail
            # again, or let the user choose another candidate.
            self.current_recipe = None
            self.state = WAITING_RECIPE_CONFIRMATION
            return self._result(WAITING_RECIPE_CONFIRMATION, True, feedback(
                f"{self.selected_candidate.title}的完整菜谱生成或校验失败，我没有换成其他菜。你可以说“重试”，或者说“换一个”。",
                f"{self.selected_candidate.title}详情失败｜重试 / 换一个",
                robot_action="show_concern", led_effect="yellow", expression="alert", question=True,
            ), selected_candidate=self.selected_candidate.as_dict())
        if not self._ensure_recipe_respects_restrictions():
            self.current_recipe = None
            self.state = PRESENTING_CANDIDATES
            return self._result(PRESENTING_CANDIDATES, True, feedback(
                "菜谱详情与刚才记录的忌口冲突，我不会让你按这个菜谱继续。请换一个候选。",
                "菜谱与忌口冲突｜请换一个",
                robot_action="show_concern", led_effect="yellow", expression="alert",
            ), recipe_candidates=[candidate.as_dict() for candidate in self.recipe_candidates])
        meats = self._raw_meat_names()
        if meats:
            self.state = WAITING_MEAT_THAW
            meat_text = "、".join(meats)
            cache_name = self._new_cache_filename()
            cache_prefix = "好的，我准备好菜谱了。" if cache_name else ""
            cache_line = "\n已准备好菜谱。" if cache_name else ""
            if cache_name and self._cache_candidate_count() > 1:
                cache_line = f"\n已准备好菜谱（同一文件含 {self._cache_candidate_count()} 个候选）。"
            return self._result(WAITING_MEAT_THAW, True, feedback(
                f"{cache_prefix}这道菜要用到{meat_text}。它已经完全解冻了吗？如果还冻着，先不要直接下锅。",
                f"确认肉类解冻：{meat_text}\n已解冻 / 还没解冻{cache_line}",
                robot_action="show_concern", led_effect="yellow", expression="alert", question=True,
            ), current_recipe=self._recipe_metadata(), recipe_cache_file=cache_name)
        return self._begin_cooking()

    def _confirm_meat_thaw(self, text: str) -> dict[str, Any]:
        compact = text.replace(" ", "")
        if any(word in compact for word in ("还没", "没有", "未解冻", "冻着")):
            return self._result(WAITING_MEAT_THAW, True, feedback(
                "最快的安全办法是用微波炉解冻档分段解冻，每次短时间检查；解冻后立刻烹饪。没有微波炉时，把密封的肉放进冷水中，每 30 分钟换水。不要放在室温台面或用热水解冻。解冻好后告诉我。",
                "肉未解冻｜微波解冻档或密封冷水解冻｜不要室温、热水解冻",
                robot_action="show_concern", led_effect="yellow", expression="alert",
            ), current_recipe=self._recipe_metadata())
        if any(word in compact for word in ("解冻好了", "解冻了", "已经解冻", "完全解冻")):
            return self._begin_cooking()
        return self._result(WAITING_MEAT_THAW, True, feedback(
            "请告诉我肉已经解冻，还是还没解冻。",
            "请选择：已解冻 / 还没解冻",
            robot_action="nod", led_effect="yellow", expression="curious", question=True,
        ))

    def _begin_cooking(self) -> dict[str, Any]:
        assert self.current_recipe is not None
        self.state, self.step_index = COOKING, 0
        ingredients = "、".join(self._ingredient_display(item) for item in self.current_recipe["ingredients"])
        items = [feedback(
            f"好的，现在开始做{self.current_recipe['name']}。我们一步一步来。",
            f"开始：{self.current_recipe['name']}",
            robot_action="encourage_gesture", led_effect="green", expression="confident",
        )]
        cache_name = self._new_cache_filename()
        if cache_name:
            count = self._cache_candidate_count()
            suffix = f"（同一文件含 {count} 个候选）" if count > 1 else ""
            items.append(feedback(
                f"这份完整菜谱已经保存到本地缓存{suffix}，下次相同需求会直接读取。",
                f"菜谱已缓存：{cache_name}{suffix}",
                robot_action="nod", led_effect="green_dynamic", expression="happy",
            ))
        items.extend((
            feedback(f"先准备：{ingredients}。食材和调料都放在手边会更从容。", f"食材：{ingredients}", robot_action="nod", led_effect="warm_white", expression="focused"),
            self._current_step_feedback(),
        ))
        return self._result(COOKING, True, *items, current_recipe=self._recipe_metadata(), current_step=1)

    def _cook(self, text: str) -> dict[str, Any]:
        if self.pending_parallel_timer_check_index is not None:
            return self._handle_parallel_timer_check(text)
        if self.pending_timer_skip_confirmation:
            return self._handle_timer_skip_confirmation(text)
        if self.pending_unstarted_timer_confirmation:
            return self._handle_unstarted_timer_confirmation(text)
        if self.pending_parallel_step_index is not None:
            return self._handle_parallel_step_confirmation(text)
        if self.pending_step_confirmation:
            markers = tuple(str(marker) for marker in self.pending_step_confirmation.get("confirmation_markers", []) if str(marker))
            if markers and self._has(text, *markers):
                confirmation = self.pending_step_confirmation
                self.pending_step_confirmation = None
                if self.step_index < len(self.current_recipe["steps"]) - 1:
                    self.step_index += 1
                step_feedback = self._current_step_feedback(
                    str(confirmation.get("confirmation_prefix", "确认完成。"))
                )
                timer_feedback = self._start_step_timer()
                items = [step_feedback]
                if timer_feedback:
                    items.append(timer_feedback)
                return self._result(COOKING, True, *items, current_step=self.step_index + 1)
            return self._result(COOKING, True, feedback(
                str(self.pending_step_confirmation.get("waiting_speech", "请先完成当前确认操作后再继续。")),
                str(self.pending_step_confirmation.get("waiting_display", "等待确认")),
                robot_action="show_concern", led_effect="yellow", expression="alert",
            ), current_step=self.step_index + 1)
        if self._has(text, "暂停", "先等一下"):
            if self.timer is not None:
                self.timer.paused_remaining_seconds = self._remaining_seconds()
            self.state = PAUSED
            return self._result(PAUSED, True, feedback("好的，烹饪指导已暂停。", "烹饪已暂停", robot_action="stop", led_effect="yellow", expression="waiting"))
        if self._has(text, "再说一遍", "重复一下"):
            return self._result(COOKING, True, self._current_step_feedback("重复："), current_step=self.step_index + 1)
        if self._has(text, "我做到哪一步", "现在做到哪一步", "做到哪一步", "当前是什么步骤", "当前步骤"):
            step = self._step()
            return self._result(COOKING, True, feedback(f"你现在在第 {self.step_index + 1}/{len(self.current_recipe['steps'])} 步。{step['instruction']}", step["display_text"], robot_action="nod", led_effect="blue", expression="focused"), current_step=self.step_index + 1)
        if self._has(text, "上一步"):
            if self.step_index == 0:
                return self._result(COOKING, True, feedback("当前已经是第一步，我为你重复一次。", self._step()["display_text"], robot_action="nod", led_effect="blue", expression="focused"), current_step=1)
            self.step_index -= 1
            return self._result(COOKING, True, self._current_step_feedback("返回："), current_step=self.step_index + 1)
        if self._has(text, "下一步", "继续", "跳过", "结束计时", "提前结束") or is_likely_next_step(text):
            if self.timer is not None and self.timer.step_index == self.step_index:
                return self._request_early_timer_end()
            if self._current_step_has_unfinished_timer():
                return self._request_unstarted_timer_confirmation()
            return self._advance_after_completion()
        if self._has(text, "开始计时", "开始煎", "开始炒", "开始煮", "开始"):
            timer_feedback = self._start_step_timer()
            if timer_feedback:
                return self._result(COOKING, True, timer_feedback, current_step=self.step_index + 1)
        if self._signals_step_timer_start(text):
            timer_feedback = self._start_step_timer()
            if timer_feedback:
                return self._result(COOKING, True, timer_feedback, current_step=self.step_index + 1)
        if self._is_parallel_prep_ack(text):
            parallel = self._parallel_prep_candidate()
            assert parallel is not None
            parallel_index, parallel_text = parallel
            self.completed_parallel_step_indexes.add(parallel_index)
            self.offered_parallel_step_indexes.discard(parallel_index)
            return self._result(COOKING, True, feedback(
                f"记住了，已记录你完成了“{parallel_text}”；当前腌制/浸泡计时继续，不会提前进入下一步。",
                "辅助准备已记录｜当前计时继续",
                robot_action="nod", led_effect="warm_white", expression="focused",
            ), current_step=self.step_index + 1)
        if self._is_waiting_prep_instruction(str(self._step().get("instruction", ""))) and self.timer is None and self._timed_step_ready_for_completion != self.step_index:
            if is_likely_timer_start(text) or is_likely_step_completion(text):
                timer_feedback = self._start_step_timer()
                if timer_feedback:
                    return self._result(COOKING, True, timer_feedback, current_step=self.step_index + 1)
        if self.timer is not None and self.timer.step_index == self.step_index and self._acknowledges_step_completion(text):
            return self._request_early_timer_end()
        if self._is_step_acknowledgment(text):
            return self._result(COOKING, True, feedback(
                self.phrases.choose("waiting_ack", WAITING_ACKNOWLEDGMENTS),
                "继续当前步骤｜完成后告诉我",
                robot_action="nod", led_effect="warm_white", expression="focused",
            ), current_step=self.step_index + 1)
        if self._acknowledges_step_completion(text):
            if self._current_step_has_unfinished_timer():
                return self._request_unstarted_timer_confirmation()
            return self._advance_after_completion()
        answer = self._answer_question(text)
        if answer:
            return answer
        return self._result(COOKING, True, feedback("我没有听懂。你可以问当前烹饪问题，或说下一步、上一步、暂停、计时、退出。", "可用命令：下一步 / 上一步 / 暂停 / 计时 / 退出", robot_action="nod", led_effect="white", expression="confused"))

    def _paused(self, text: str) -> dict[str, Any]:
        if self._has(text, "继续", "恢复", "继续做"):
            self.state = COOKING
            if self.timer is not None and self.timer.paused_remaining_seconds is not None:
                self.timer.deadline = self.clock() + self.timer.paused_remaining_seconds
                self.timer.paused_remaining_seconds = None
            return self._result(COOKING, True, feedback("已恢复烹饪指导。", "已恢复当前步骤", robot_action="nod", led_effect="blue_dynamic", expression="focused"), self._current_step_feedback("继续当前步骤："), current_step=self.step_index + 1)
        answer = self._answer_question(text)
        if answer:
            return answer
        return self._result(PAUSED, True, feedback("现在仍处于暂停状态。你可以说继续、询问问题、查询计时或退出。", "烹饪已暂停", robot_action="stop", led_effect="yellow", expression="waiting"))

    def _answer_question(self, text: str) -> dict[str, Any] | None:
        if not self.current_recipe:
            return None
        # Recipe source and Q&A source are independent: a cached/local recipe
        # must still be able to use the configured Agent for questions.
        context = CookingContext(
            recipe=self.current_recipe, current_step=self._step(), servings=self.request.servings,
            taste_preferences=self.request.taste_preferences, dietary_restrictions=self.request.dietary_restrictions,
            available_ingredients=self.request.available_ingredients, available_equipment=self.request.available_equipment,
            timer_remaining_seconds=self._remaining_seconds(), conversation_summary=self.conversation_summary[-5:],
        )
        if self._will_use_agent_for_question(text, context):
            self._emit_progress(feedback(
                "请稍后，我正在思考要怎么应对。",
                "请稍后，我正在思考要怎么应对",
                robot_action="nod", led_effect="blue_dynamic", expression="focused",
            ))
        answer = self.question_service.answer(text, context)
        if answer is None:
            return None
        self.conversation_summary.append(f"问：{text} 答：{answer.answer}")
        next_state = PAUSED if answer.should_pause_cooking else self.state
        if answer.should_pause_cooking:
            self.state = PAUSED
        answer_feedback = feedback(answer.answer, answer.display_text, robot_action=answer.robot_action, led_effect=answer.led_effect, expression=answer.expression)
        if answer.safety_level == "NORMAL":
            answer_feedback["speech"] = f"我想一想，结合你现在这一步来判断。{answer.answer}"
            answer_feedback["display"] = f"我在陪你一起判断\n{answer.display_text}"
            answer_feedback["robot_action"] = "nod"
            answer_feedback["led_effect"] = "green_dynamic"
            answer_feedback["expression"] = "focused"
        return self._result(next_state, True, answer_feedback, current_step=self.step_index + 1, safety_level=answer.safety_level)

    def _will_use_agent_for_question(self, text: str, context: CookingContext) -> bool:
        """Show thinking only for questions that will leave the local rules."""
        client = getattr(self.question_service, "llm_client", None)
        is_available = getattr(client, "is_available", None)
        if not callable(is_available) or not is_available():
            return False
        return self.offline_question_service.answer(text, context) is None

    def _handle_timer(self, text: str) -> dict[str, Any] | None:
        if "取消计时" in text:
            self.timer = None
            self.pending_timer_skip_confirmation = False
            return self._result(self.state, True, feedback("计时已取消。", "计时已取消", robot_action="stop", led_effect="white", expression="neutral"))
        if self._has(text, "继续计时", "继续倒计时", "不要结束"):
            if self.timer is None:
                return self._result(self.state, True, feedback("当前没有正在运行的计时。", "暂无计时", robot_action="nod", led_effect="white", expression="neutral"))
            return self._timer_still_running_response()
        seconds = extract_timer_seconds(text)
        if seconds is not None:
            self.timer = Timer(self.clock() + seconds, seconds, label="烹饪", step_index=self.step_index)
            return self._result(self.state, True, feedback(f"已开始计时 {self._format_seconds(seconds)}。", f"倒计时：{self._format_seconds(seconds)}", robot_action="nod", led_effect="blue", expression="focused"))
        if self._has(text, "还有多久", "计时结束了吗"):
            if self.timer is None:
                return self._result(self.state, True, feedback("当前没有正在运行的计时。", "暂无计时", robot_action="nod", led_effect="white", expression="neutral"))
            remaining = self._remaining_seconds() or 0
            return self._result(self.state, True, feedback(f"还剩 {self._format_seconds(remaining)}。", f"计时剩余：{self._format_seconds(remaining)}", robot_action="nod", led_effect="blue", expression="focused"))
        return None

    def _timer_end_if_due(self) -> dict[str, Any] | None:
        if self.timer and self.timer.paused_remaining_seconds is not None:
            return None
        if self.timer and self.clock() >= self.timer.deadline:
            timer = self.timer
            self.timer = None
            self.pending_timer_skip_confirmation = False
            if timer.step_index is not None and timer.step_index == self.step_index and self.current_recipe:
                self._timed_step_ready_for_completion = self.step_index
                step = self._step()
                offered_index = self.parallel_offer_by_timer_step.get(timer.step_index)
                if (
                    offered_index is not None
                    and offered_index in self.offered_parallel_step_indexes
                    and self._is_waiting_prep_instruction(str(step.get("instruction", "")))
                ):
                    self.pending_parallel_timer_check_index = offered_index
                    parallel_instruction = str(self.current_recipe["steps"][offered_index]["instruction"])
                    return self._result(COOKING, True, feedback(
                        f"第 {self.step_index + 1} 步计时结束，请先检查当前腌制/浸泡状态。刚才计时时，你已经同步完成了“{parallel_instruction}”这一步，对吧？请说“对”或“还没完成”。",
                        "计时结束｜并行准备完成了吗？",
                        robot_action="nod", led_effect="yellow", expression="focused",
                    ), current_step=self.step_index + 1)
                action = str(step.get("timer_end_action", ""))
                if action == "await_confirmation":
                    self.pending_step_confirmation = step
                    return self._result(COOKING, True, feedback(
                        str(step.get("timer_end_speech", "计时结束，请完成下一项确认操作。")),
                        str(step.get("timer_end_display", "计时结束｜等待确认")),
                        robot_action="show_concern", led_effect="yellow", expression="alert",
                    ), current_step=self.step_index + 1)
                if action == "advance":
                    if self.step_index < len(self.current_recipe["steps"]) - 1:
                        self.step_index += 1
                    return self._result(COOKING, True,
                        feedback(str(step.get("timer_end_speech", "计时结束，请继续下一步。")), str(step.get("timer_end_display", "计时结束｜进入下一步")), robot_action="nod", led_effect="green_dynamic", expression="focused"),
                        self._current_step_feedback(), current_step=self.step_index + 1,
                    )
            return self._result(self.state, True, self._timer_completion_feedback(timer), current_step=self.step_index + 1)
        return None

    def _timer_completion_feedback(self, timer: Timer) -> dict[str, str]:
        """Turn a timer alarm into a doneness check, never an auto-advance."""
        step = self._step() if self.current_recipe else {}
        instruction = str(step.get("instruction") or "").strip()
        if self._is_waiting_prep_instruction(instruction):
            speech = (
                f"计时结束，{self._format_seconds(timer.seconds)}的准备时间到了。可以检查腌制/浸泡状态；"
                "如果已经完成，就告诉我“做好了”，我再进入下一步。"
            )
        elif any(word in instruction for word in ("肉", "排骨", "鸡", "鱼", "虾")):
            speech = (
                f"计时结束，{self._format_seconds(timer.seconds)}下限时间到了。请检查食材是否完全变色、中心无粉红；"
                "如果还没达到，就继续中火翻炒，达到后告诉我“做好了”。"
            )
        else:
            speech = (
                f"计时结束，{self._format_seconds(timer.seconds)}参考时间到了。请按步骤中的状态检查食材；"
                "达到后告诉我“做好了”，不要只按时间判断。"
            )
        return feedback(
            speech,
            f"计时结束｜请检查：{instruction}\n完成后说：做好了",
            robot_action="show_concern", led_effect="yellow", expression="warning",
        )

    def _start_step_timer(self, label: str | None = None) -> dict[str, str] | None:
        step = self._step()
        seconds = step.get("duration_seconds")
        if not isinstance(seconds, (int, float)) or seconds <= 0:
            return None
        seconds = int(seconds)
        self._timed_step_ready_for_completion = None
        if label is None:
            label = str(step.get("timer_label") or f"第 {self.step_index + 1} 步")
        self.timer = Timer(self.clock() + seconds, seconds, label=label, step_index=self.step_index)
        parallel_candidate = (
            self._parallel_prep_candidate()
            if self._is_waiting_prep_instruction(str(step.get("instruction", "")))
            else None
        )
        if parallel_candidate is not None:
            parallel_index, _ = parallel_candidate
            self.offered_parallel_step_indexes.add(parallel_index)
            self.parallel_offer_by_timer_step[self.step_index] = parallel_index
        parallel_hint = self._parallel_prep_hint(parallel_candidate)
        speech = f"好的，{label}开始计时 {self._format_seconds(seconds)}。我会在时间到时提醒你。"
        display = f"{label}倒计时：{self._format_seconds(seconds)}"
        if parallel_hint:
            speech = f"{speech} {parallel_hint}"
            display = f"{display}\n{parallel_hint}"
        return feedback(
            speech,
            display,
            robot_action="nod", led_effect="green_dynamic", expression="focused",
        )

    def _request_early_timer_end(self) -> dict[str, Any]:
        self.pending_timer_skip_confirmation = True
        remaining = self._format_seconds(self._remaining_seconds() or 0)
        return self._result(COOKING, True, feedback(
            f"现在第 {self.step_index + 1} 步正在计时，还剩约 {remaining}。确定要结束计时并前往下一步吗？请说“确认”或“继续计时”。",
            "计时进行中｜确认结束计时？",
            robot_action="show_concern", led_effect="yellow", expression="alert",
        ), current_step=self.step_index + 1)

    def _handle_timer_skip_confirmation(self, text: str) -> dict[str, Any]:
        compact = text.replace(" ", "")
        if self._is_affirmative(compact):
            self.pending_timer_skip_confirmation = False
            self.timer = None
            prefix = feedback(
                "好的，已按你的确认提前结束计时，进入下一步。",
                "已结束计时｜进入下一步",
                robot_action="nod", led_effect="green_dynamic", expression="focused",
            )
            return self._with_prefix(prefix, self._advance_after_completion())
        if self._has(compact, "继续计时", "不要", "不结束", "继续等", "取消"):
            self.pending_timer_skip_confirmation = False
            return self._timer_still_running_response()
        return self._result(COOKING, True, feedback(
            "当前计时仍在进行。请说“确认”提前结束并前往下一步，或说“继续计时”。",
            "等待确认｜结束计时？",
            robot_action="nod", led_effect="yellow", expression="alert",
        ), current_step=self.step_index + 1)

    def _current_step_has_unfinished_timer(self) -> bool:
        """Return whether the current timed step has neither run nor finished.

        A duration is part of the cooking safety contract. Short commands such
        as “下一步” must not silently bypass it before the user starts the
        timer or explicitly confirms that they timed and checked it elsewhere.
        """
        seconds = self._step().get("duration_seconds")
        return (
            isinstance(seconds, (int, float))
            and seconds > 0
            and self.timer is None
            and self._timed_step_ready_for_completion != self.step_index
        )

    def _request_unstarted_timer_confirmation(self) -> dict[str, Any]:
        self.pending_unstarted_timer_confirmation = True
        seconds = int(self._step()["duration_seconds"])
        return self._result(COOKING, True, feedback(
            f"当前第 {self.step_index + 1} 步建议计时 {self._format_seconds(seconds)}，但计时还没有启动。"
            "如果你已经自行计时并检查完成，请说“确认完成”；要使用助手计时，请说“开始计时”。",
            "当前步骤尚未计时｜确认完成 / 开始计时",
            robot_action="show_concern", led_effect="yellow", expression="alert",
        ), current_step=self.step_index + 1)

    def _handle_unstarted_timer_confirmation(self, text: str) -> dict[str, Any]:
        compact = text.replace(" ", "")
        if self._has(compact, "开始计时", "给我计时", "启动计时"):
            self.pending_unstarted_timer_confirmation = False
            timer_feedback = self._start_step_timer()
            assert timer_feedback is not None
            return self._result(COOKING, True, timer_feedback, current_step=self.step_index + 1)
        if self._has(compact, "确认完成", "已经计时并完成", "自行计时完成", "我自己计时了"):
            self.pending_unstarted_timer_confirmation = False
            return self._advance_after_completion()
        if self._has(compact, "取消", "继续当前步骤", "还没完成", "不跳过"):
            self.pending_unstarted_timer_confirmation = False
            return self._result(COOKING, True, self._current_step_feedback("好的，继续当前步骤："), current_step=self.step_index + 1)
        return self._result(COOKING, True, feedback(
            "请说“确认完成”表示你已自行计时并检查完成，或说“开始计时”使用助手计时。",
            "等待确认｜确认完成 / 开始计时",
            robot_action="nod", led_effect="yellow", expression="alert",
        ), current_step=self.step_index + 1)

    def _handle_parallel_step_confirmation(self, text: str) -> dict[str, Any]:
        assert self.pending_parallel_step_index is not None
        compact = text.replace(" ", "")
        parallel_index = self.pending_parallel_step_index
        if self._has(compact, "还没", "没有", "不对", "没做", "没完成"):
            self.completed_parallel_step_indexes.discard(parallel_index)
            self.offered_parallel_step_indexes.discard(parallel_index)
            self.pending_parallel_step_index = None
            return self._result(COOKING, True, self._current_step_feedback("好的，那我们现在完成："), current_step=self.step_index + 1)
        if self._is_affirmative(compact) or compact in {"对", "对的", "是", "是的"}:
            self.completed_parallel_step_indexes.add(parallel_index)
            self.offered_parallel_step_indexes.discard(parallel_index)
            self.pending_parallel_step_index = None
            return self._advance_after_completion()
        return self._result(COOKING, True, feedback(
            "刚才计时期间这一步已记录为完成。请说“对”跳过它，或说“还没完成”现在来做。",
            "等待确认｜并行准备是否已完成？",
            robot_action="nod", led_effect="warm_white", expression="focused",
        ), current_step=self.step_index + 1)

    def _handle_parallel_timer_check(self, text: str) -> dict[str, Any]:
        """Ask proactively about work suggested during a completed timer."""
        assert self.pending_parallel_timer_check_index is not None
        parallel_index = self.pending_parallel_timer_check_index
        compact = text.replace(" ", "")
        if self._has(compact, "还没", "没有", "不对", "没做", "没完成"):
            self.offered_parallel_step_indexes.discard(parallel_index)
            self.pending_parallel_timer_check_index = None
            return self._result(COOKING, True, feedback(
                "好的，先不用做并行准备。请检查当前计时步骤，确认完成后告诉我“做好了”。",
                "并行准备未完成｜继续当前步骤",
                robot_action="nod", led_effect="warm_white", expression="focused",
            ), current_step=self.step_index + 1)
        if self._is_affirmative(compact) or compact in {"对", "对的", "是", "是的"}:
            self.completed_parallel_step_indexes.add(parallel_index)
            self.offered_parallel_step_indexes.discard(parallel_index)
            self.pending_parallel_timer_check_index = None
            return self._result(COOKING, True, feedback(
                "好的，我已记下这项并行准备完成。请检查当前计时步骤；确认腌制/浸泡也完成后，告诉我“做好了”，我会跳过这项已完成的准备。",
                "已记录并行准备｜继续检查当前步骤",
                robot_action="nod", led_effect="green_dynamic", expression="focused",
            ), current_step=self.step_index + 1)
        return self._result(COOKING, True, feedback(
            "刚才计时时的并行准备是否完成？请说“对”或“还没完成”。",
            "等待确认｜并行准备完成了吗？",
            robot_action="nod", led_effect="yellow", expression="focused",
        ), current_step=self.step_index + 1)

    def _timer_still_running_response(self) -> dict[str, Any]:
        return self._result(COOKING, True, feedback(
            "当前步骤的计时还没结束，先继续观察和操作；计时结束后检查状态，再告诉我“做好了”。",
            f"计时进行中｜剩余约 {self._format_seconds(self._remaining_seconds() or 0)}",
            robot_action="nod", led_effect="blue_dynamic", expression="focused",
        ), current_step=self.step_index + 1)

    def _current_step_feedback(self, prefix: str = "") -> dict[str, str]:
        step = self._step()
        instruction = step["instruction"]
        timer_hint = self._step_timer_hint(step)
        parallel_hint = self._parallel_prep_hint() if self._timer_is_running_for_current_step() else None
        if timer_hint:
            instruction = f"{instruction} {timer_hint}"
        if parallel_hint:
            instruction = f"{instruction} {parallel_hint}"
        if step.get("safety_note"):
            instruction += f" 注意：{step['safety_note']}"
        display = step["display_text"]
        if step.get("safety_note"):
            display = f"{display}\n注意：{step['safety_note']}"
        if timer_hint:
            display = f"{display}\n{timer_hint}"
        if parallel_hint:
            display = f"{display}\n{parallel_hint}"
        return feedback(f"{prefix}{instruction}", display, robot_action=step["robot_action"], led_effect=step["led_effect"], expression=step["expression"])

    def _step_timer_hint(self, step: dict[str, Any]) -> str | None:
        seconds = step.get("duration_seconds")
        if not isinstance(seconds, (int, float)) or seconds <= 0:
            return None
        instruction = str(step.get("instruction", ""))
        if self._is_waiting_prep_instruction(instruction):
            return f"准备计时 {self._format_seconds(int(seconds))}；开始腌制/浸泡后说“开始了”或“给我计时”，我就开始计时。"
        if not any(word in instruction for word in ("预热", "煮", "炖", "焖", "煎", "烤", "蒸", "炸", "焯", "炒", "收汁", "加热")):
            return None
        return f"准备计时 {self._format_seconds(int(seconds))}；食材下锅后说“下锅了”或“开始”，我就开始计时。"

    def _signals_step_timer_start(self, text: str) -> bool:
        compact = text.replace(" ", "")
        if any(marker in compact for marker in ("吗", "？", "?")):
            return False
        step = self._step() if self.current_recipe else {}
        if not self._step_timer_hint(step):
            return False
        if is_likely_timer_start(text):
            return True
        # Accept action reports beyond explicit timer requests.
        action_markers = (
            "下锅", "倒入", "放入", "入锅", "开始翻炒", "开始腌制", "腌上了", "已经开始", "开始了", "开始啦", "开始喽", "开炒了", "开煎了", "现在开始", "可以计时",
        )
        timer_markers = ("计时", "倒计时", "给我计时", "帮我计时", "开始计时", "开始倒计时", "帮忙计时", "记一下时间", "计一下时")
        return any(marker in compact for marker in action_markers + timer_markers)

    def _parallel_prep_candidate(self) -> tuple[int, str] | None:
        """Return one safe future prep step that has not already been done."""
        if not self.current_recipe:
            return None
        for index, candidate in enumerate(self.current_recipe.get("steps", [])[self.step_index + 1:], start=self.step_index + 1):
            if index in self.completed_parallel_step_indexes:
                continue
            candidate_text = str(candidate.get("instruction", ""))
            if self._is_waiting_prep_instruction(candidate_text):
                continue
            # While meat marinates, boiling plain water in a separate pot is
            # safe and useful.  It is the one heating action we deliberately
            # permit; heating oil or cooking food still has to wait.
            if self._is_safe_water_boiling_prep(candidate_text):
                return index, candidate_text
            if self._reuses_waiting_step_ingredients(candidate_text, str(self._step().get("instruction", ""))):
                continue
            if self._contains_heat_action(candidate_text) or self._is_final_cooking_step(candidate_text):
                continue
            if self._is_actionable_parallel_prep(candidate_text):
                return index, candidate_text
        return None

    def _reuses_waiting_step_ingredients(self, candidate: str, waiting_step: str) -> bool:
        """Reject prep that reuses anything already measured into this step."""
        if not self.current_recipe:
            return False
        ingredient_names = [
            str(item.get("name", "")).strip()
            for item in self.current_recipe.get("ingredients", [])
            if str(item.get("name", "")).strip()
        ]
        waiting_names = {name for name in ingredient_names if name in waiting_step}
        candidate_names = {name for name in ingredient_names if name in candidate}
        return bool(candidate_names & waiting_names)

    @staticmethod
    def _is_actionable_parallel_prep(instruction: str) -> bool:
        """A parallel hint must name an actual prep action, not a placeholder."""
        seasoning_terms = ("生抽", "老抽", "米醋", "醋", "白砂糖", "白糖", "盐", "料酒", "蚝油", "胡椒")
        cutting_or_washing = ("洗", "切", "去皮", "切块", "切丝", "切片", "切丁")
        return any(term in instruction for term in seasoning_terms + cutting_or_washing)

    @staticmethod
    def _is_safe_water_boiling_prep(instruction: str) -> bool:
        """Allow only a standalone pot of water to heat during a marinade."""
        has_water = "水" in instruction
        has_pot = any(term in instruction for term in ("锅", "水壶"))
        has_boiling_action = any(term in instruction for term in ("烧开", "煮沸", "沸腾", "加热"))
        unsafe_or_dependent = (
            "油", "炒", "煎", "炸", "炖", "焖", "蒸", "烤", "焯", "收汁",
            "关火", "盛出", "装盘", "放入", "加入", "下入", "后", "再", "然后",
        )
        return has_water and has_pot and has_boiling_action and not any(term in instruction for term in unsafe_or_dependent)

    @staticmethod
    def _is_final_cooking_step(instruction: str) -> bool:
        return any(term in instruction for term in ("关火", "盛出", "装盘", "出锅"))

    def _parallel_prep_hint(self, candidate: tuple[int, str] | None = None) -> str | None:
        """Suggest a safe, non-advancing prep task during a timed marinade."""
        if not self.current_recipe:
            return None
        current = self._step()
        instruction = str(current.get("instruction", ""))
        if not self._is_waiting_prep_instruction(instruction):
            return None
        candidate = candidate if candidate is not None else self._parallel_prep_candidate()
        if candidate:
            _, candidate_text = candidate
            if self._is_safe_water_boiling_prep(candidate_text):
                return (
                    f"等待的时候可以按照时间规划，同步做这一步：{candidate_text}。这一步只烧清水，不接触正在腌制的肉；"
                    "水沸后说“准备好了”，当前腌制计时不会被打断。"
                )
            return f"等待期间可以先按照你的时间规划准备下一步：{candidate_text}。只做不动火的准备，不要提前热油或开火；当前计时不会被打断。"
        return FREE_WAIT_DURING_TIMER_HINT

    def _timer_is_running_for_current_step(self) -> bool:
        return self.timer is not None and self.timer.step_index == self.step_index

    @staticmethod
    def _contains_heat_action(instruction: str) -> bool:
        return any(word in instruction for word in ("预热", "热油", "加油", "炒", "煎", "炸", "炖", "焖", "蒸", "烤", "烧", "煮", "焯", "加热", "收汁", "开火"))

    def _is_parallel_prep_ack(self, text: str) -> bool:
        if not self.current_recipe or not self._timer_is_running_for_current_step():
            return False
        instruction = str(self._step().get("instruction", ""))
        if not self._is_waiting_prep_instruction(instruction):
            return False
        compact = text.replace(" ", "")
        if any(word in compact for word in ("腌制好了", "腌好了", "浸泡好了", "时间到了", "计时结束")):
            return False
        if any(marker in compact for marker in ("吗", "？", "?", "怎么", "如何")):
            return False
        candidate = self._parallel_prep_candidate()
        if candidate is None:
            return False
        _, candidate_text = candidate
        if any(word in compact for word in ("准备好了", "调料好了", "配料好了", "食材好了")):
            return True

        # Do not require one fixed acknowledgement.  A user normally refers
        # to the thing they just prepared, for example “糖醋汁调好了” or
        # “酱汁我弄完了”.  A generic “完成” remains reserved for the timed
        # marinade itself, so it cannot silently skip a real recipe step.
        prep_terms = (
            "糖醋汁", "调味汁", "酱汁", "调料", "配料", "小碗",
            "生抽", "老抽", "米醋", "醋", "白砂糖", "白糖", "盐", "料酒",
            "清水", "水", "烧开", "沸腾",
        )
        mentioned_terms = [term for term in prep_terms if term in candidate_text]
        return bool(mentioned_terms) and is_likely_step_completion(text) and any(
            term in compact for term in mentioned_terms
        )

    @staticmethod
    def _is_waiting_prep_instruction(instruction: str) -> bool:
        """Distinguish “腌制10分钟” from “倒入腌制好的鸡肉丁”."""
        if any(word in instruction for word in ("浸泡", "泡发", "静置", "醒发")):
            return True
        return bool(re.search(r"腌制(?!好)", instruction))

    @staticmethod
    def _ingredient_display(item: dict[str, Any]) -> str:
        amount = str(item.get("amount") or "适量")
        unit = str(item.get("unit") or "")
        return f"{item.get('name', '食材')} {amount}{unit}"

    @staticmethod
    def _acknowledges_step_completion(text: str) -> bool:
        """Recognize a user's clear report that the active manual step is done.

        This is deliberately local and conservative: a question such as
        “切好了吗？” must never move a real cooking session forward.
        """
        return is_likely_step_completion(text)

    def _advance_after_completion(self) -> dict[str, Any]:
        assert self.current_recipe is not None
        completed_number = self.step_index + 1
        self._timed_step_ready_for_completion = None
        if self.step_index >= len(self.current_recipe["steps"]) - 1:
            self.state = COMPLETED
            return self._result(COMPLETED, False, self._finished_feedback(), current_recipe=self._recipe_metadata())
        skipped_parallel_steps: list[int] = []
        while self.step_index < len(self.current_recipe["steps"]) - 1:
            self.step_index += 1
            if self.step_index in self.completed_parallel_step_indexes:
                skipped_parallel_steps.append(self.step_index)
                continue
            if self.step_index in self.offered_parallel_step_indexes:
                self.pending_parallel_step_index = self.step_index
                instruction = self._step()["instruction"]
                return self._result(COOKING, True, feedback(
                    f"刚才计时时，你已经同步完成了“{instruction}”这一步，对吧？说“对”就跳过它；如果还没完成，请说“还没完成”。",
                    "并行准备确认｜刚才完成了吗？",
                    robot_action="nod", led_effect="warm_white", expression="focused",
                ), current_step=self.step_index + 1)
            break
        if self.step_index >= len(self.current_recipe["steps"]) - 1 and self.step_index in self.completed_parallel_step_indexes:
            self.state = COMPLETED
            return self._result(COMPLETED, False, self._finished_feedback(), current_recipe=self._recipe_metadata())
        encouragement = self.phrases.choose("step_encouragement", STEP_ENCOURAGEMENTS)
        skipped_text = ""
        if skipped_parallel_steps:
            numbers = "、".join(str(index + 1) for index in skipped_parallel_steps)
            skipped_text = f"已跳过计时期间确认完成的第 {numbers} 步。"
        return self._result(
            COOKING,
            True,
            feedback(
                f"{encouragement} 已完成第 {completed_number} 步。{skipped_text}接下来进行第 {self.step_index + 1} 步。",
                f"已完成第 {completed_number} 步，进入第 {self.step_index + 1} 步",
                robot_action="encourage_gesture", led_effect="green_dynamic", expression="happy",
            ),
            self._current_step_feedback(),
            current_step=self.step_index + 1,
        )

    def _finished_feedback(self) -> dict[str, str]:
        assert self.current_recipe is not None
        return feedback(
            self.phrases.choose("finished", FINISHED_RESPONSES).format(dish=self.current_recipe["name"]),
            f"{self.current_recipe['name']}完成",
            robot_action="high_five", led_effect="rainbow", expression="excited",
        )

    def _step(self) -> dict[str, Any]:
        assert self.current_recipe is not None
        return self.current_recipe["steps"][self.step_index]

    def _ask_ingredients(self) -> dict[str, str]:
        return feedback("你现在有哪些食材？可以直接说鸡蛋、番茄、面条之类的。", "请说出已有食材，例如：鸡蛋、番茄、面条", robot_action="nod", led_effect="blue", expression="curious", question=True)

    def _candidate_display(self) -> str:
        mode = self._provider_mode()
        lines = ["我为你生成的菜谱" if mode == "ai_generated" else ("本地缓存菜谱" if mode == "local_cache" else "推荐菜谱（离线示例）")]
        for index, candidate in enumerate(self.recipe_candidates, start=1):
            if not self.request.available_ingredients:
                supply = "食材见详情"
            else:
                supply = "现有食材足够" if not candidate.missing_ingredients else f"缺：{'、'.join(candidate.missing_ingredients)}"
            lines.append(f"{index}. {candidate.title}｜{candidate.estimated_minutes or '?'} 分钟｜{candidate.difficulty}｜{supply}")
        return "\n".join(lines)

    def _recipe_metadata(self) -> dict[str, Any] | None:
        if not self.current_recipe:
            return None
        return {key: self.current_recipe.get(key) for key in ("recipe_id", "name", "source_name", "source_url", "estimated_time_minutes", "difficulty")}

    def _provider_mode(self) -> str:
        return str(getattr(self._active_provider, "mode", "mock"))

    def _remaining_seconds(self) -> int | None:
        if self.timer is None:
            return None
        if self.timer.paused_remaining_seconds is not None:
            return self.timer.paused_remaining_seconds
        return max(0, int(self.timer.deadline - self.clock() + 0.999))

    def _result(self, state: str, active: bool, *items: dict[str, Any], **metadata: Any) -> dict[str, Any]:
        metadata.setdefault("provider_mode", self._provider_mode())
        return self._public_metadata(result(state, active, *items, **metadata))

    def _with_prefix(self, prefix: dict[str, Any], response: dict[str, Any]) -> dict[str, Any]:
        if "steps" in response:
            response["steps"] = [prefix, *response["steps"]]
        else:
            primary = {key: response.pop(key) for key in ("speech", "question", "display", "robot_action", "led_effect", "expression") if key in response}
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

    @staticmethod
    def _format_seconds(seconds: int) -> str:
        return f"{seconds // 60} 分钟" if seconds >= 60 and seconds % 60 == 0 else f"{seconds} 秒"

    def _ensure_recipe_respects_restrictions(self) -> bool:
        if not self.current_recipe:
            return False
        names = [str(item.get("name", "")) for item in self.current_recipe.get("ingredients", [])]
        return not ingredient_conflicts(names, self.request.dietary_restrictions)

    def _raw_meat_names(self) -> list[str]:
        if not self.current_recipe:
            return []
        found: list[str] = []
        for item in self.current_recipe.get("ingredients", []):
            name = str(item.get("name", ""))
            if any(word in name for word in RAW_MEAT_TERMS) and name not in found:
                found.append(name)
        return found

    def _new_cache_filename(self) -> str | None:
        path = getattr(self._active_provider, "last_cache_path", None)
        return path.name if isinstance(path, Path) else None

    def _cache_candidate_count(self) -> int:
        count = getattr(self._active_provider, "last_cache_candidate_count", 0)
        return int(count) if isinstance(count, int) else 0

    @staticmethod
    def _is_affirmative(text: str) -> bool:
        compact = text.replace(" ", "")
        return any(word in compact for word in ("可以", "开始", "就这个", "按这个做", "确认", "好的", "好呀", "行", "没问题", "重试", "重新生成"))

    @staticmethod
    def _is_gratitude(text: str) -> bool:
        compact = text.replace(" ", "")
        return compact in {"谢谢", "谢谢你", "多谢", "感谢", "辛苦了"}

    @staticmethod
    def _is_step_acknowledgment(text: str) -> bool:
        return text.replace(" ", "") in {"好", "好的", "好吧", "行", "知道了", "明白了", "收到"}

    @classmethod
    def _public_metadata(cls, value: Any) -> Any:
        """Hide internal provider branding from user-facing runtime results."""
        if isinstance(value, list):
            return [cls._public_metadata(item) for item in value]
        if isinstance(value, dict):
            cleaned = {key: cls._public_metadata(item) for key, item in value.items()}
            if cleaned.get("source_name") == "豆包 AI 生成":
                cleaned.pop("source_name", None)
            return cleaned
        return value
