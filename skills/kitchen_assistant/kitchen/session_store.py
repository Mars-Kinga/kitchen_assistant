from __future__ import annotations

import time
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .cooking_question_service import RuleBasedCookingQuestionService, STOP_AND_CHECK
from .dietary_rules import ingredient_conflicts
from .intent_parser import extract_serving_choice, extract_timer_seconds
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
        self.awaiting_flip_confirmation = False
        self.phrases = RandomPhrasePicker()
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

        if self._has(text, "退出", "取消", "不做了") and "取消计时" not in text:
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
        if self._is_steak_request() and not self.request.steak_doneness:
            return self._result(COLLECTING_PREFERENCES, True, feedback(
                "这块牛排希望几成熟？可以说三分熟、五分熟、七分熟或全熟。",
                "请选择熟度：三分 / 五分 / 七分 / 全熟",
                robot_action="nod", led_effect="blue", expression="curious", question=True,
            ))
        if self._is_steak_request() and self.request.steak_thickness_cm is None:
            return self._result(COLLECTING_PREFERENCES, True, feedback(
                "牛排大约多厚？可以说 2 厘米，或者说普通厚度。",
                "请选择厚度：约 2 厘米 / 直接说实际厚度",
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
        try:
            self.recipe_candidates = self._active_provider.search_recipes(self.request)
        except Exception:
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
                "我找到了之前保存的本地菜谱，这次不用重新生成，会更快也不会消耗菜谱生成 token。",
                "已命中本地菜谱缓存",
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
            speech = "没有找到与指定菜名匹配的离线菜谱，我不会用无关菜谱替代。请配置生成服务，或换个菜名。" if self.request.requested_dish else "没有找到合适的离线菜谱。你可以补充食材或换个菜名。"
            return self._result(PRESENTING_CANDIDATES, True, searching, feedback(speech, "暂无候选｜没有匹配菜谱", robot_action="nod", led_effect="white", expression="neutral"), recipe_candidates=[], provider_mode=actual_mode)
        candidate_feedback = feedback(
            "我列出了几个建议，想吃哪个呢？你可以说第一个、第二个或者直接说菜名。" if actual_mode == "ai_generated" else "我找到了几个本地菜谱。你可以说第一个、第二个或者直接说菜名。",
            self._candidate_display(), robot_action="nod", led_effect="green_dynamic", expression="happy",
        )
        items = [searching]
        if self._used_provider_fallback:
            fallback_speech = (
                "生成服务暂时不可用，我先使用本地示例菜谱为你推荐。"
                if configured_ai else "目前没有配置真实联网服务，我先使用本地示例菜谱为你推荐。"
            )
            items.append(feedback(fallback_speech, "已切换离线菜谱模式", robot_action="nod", led_effect="warm_white", expression="neutral"))
        items.append(candidate_feedback)
        return self._result(PRESENTING_CANDIDATES, True, *items, recipe_candidates=[candidate.as_dict() for candidate in self.recipe_candidates], provider_mode=self._provider_mode())

    def _presenting_candidates(self, text: str) -> dict[str, Any]:
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
        display = f"{candidate.title}\n{source}\n{candidate.estimated_minutes or '未知'} 分钟｜{candidate.difficulty}\n主要食材：{'、'.join(candidate.main_ingredients)}\n{inventory}"
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
            self.current_recipe = self.normalizer.normalize(self._active_provider.get_recipe_detail(self.selected_candidate))
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
            items.append(feedback(
                "这份完整菜谱已经保存到本地缓存，下次相同需求会直接读取。",
                f"菜谱已缓存：{cache_name}",
                robot_action="nod", led_effect="green_dynamic", expression="happy",
            ))
        items.extend((
            feedback(f"先准备：{ingredients}。食材和调料都放在手边会更从容。", f"食材：{ingredients}", robot_action="nod", led_effect="warm_white", expression="focused"),
            self._current_step_feedback(),
        ))
        return self._result(COOKING, True, *items, current_recipe=self._recipe_metadata(), current_step=1)

    def _cook(self, text: str) -> dict[str, Any]:
        if self.awaiting_flip_confirmation:
            if self._has(text, "翻面好了", "已经翻面", "翻好了"):
                self.awaiting_flip_confirmation = False
                if self.step_index < len(self.current_recipe["steps"]) - 1:
                    self.step_index += 1
                step_feedback = self._current_step_feedback("翻面完成。")
                timer_feedback = self._start_step_timer(label="另一面")
                items = [step_feedback]
                if timer_feedback:
                    items.append(timer_feedback)
                return self._result(COOKING, True, *items, current_step=self.step_index + 1)
            return self._result(COOKING, True, feedback(
                "请先安全翻面，翻好后告诉我“翻面好了”，我再开始另一面计时。",
                "等待翻面确认",
                robot_action="show_concern", led_effect="yellow", expression="alert",
            ), current_step=self.step_index + 1)
        if self._has(text, "暂停", "先等一下"):
            if self.timer is not None:
                self.timer.paused_remaining_seconds = self._remaining_seconds()
            self.state = PAUSED
            return self._result(PAUSED, True, feedback("好的，烹饪指导已暂停。", "烹饪已暂停", robot_action="stop", led_effect="yellow", expression="waiting"))
        if self._has(text, "再说一遍", "重复一下"):
            return self._result(COOKING, True, self._current_step_feedback("重复："), current_step=self.step_index + 1)
        if self._has(text, "我做到哪一步", "当前是什么步骤", "当前步骤"):
            step = self._step()
            return self._result(COOKING, True, feedback(f"你现在在第 {self.step_index + 1}/{len(self.current_recipe['steps'])} 步。{step['instruction']}", step["display_text"], robot_action="nod", led_effect="blue", expression="focused"), current_step=self.step_index + 1)
        if self._has(text, "上一步"):
            if self.step_index == 0:
                return self._result(COOKING, True, feedback("当前已经是第一步，我为你重复一次。", self._step()["display_text"], robot_action="nod", led_effect="blue", expression="focused"), current_step=1)
            self.step_index -= 1
            return self._result(COOKING, True, self._current_step_feedback("返回："), current_step=self.step_index + 1)
        if self._has(text, "下一步", "继续"):
            if self.step_index >= len(self.current_recipe["steps"]) - 1:
                self.state = COMPLETED
                return self._result(COMPLETED, False, self._finished_feedback(), current_recipe=self._recipe_metadata())
            self.step_index += 1
            return self._result(COOKING, True, self._current_step_feedback(), current_step=self.step_index + 1)
        if self._has(text, "开始计时", "开始煎", "开始炒", "开始煮"):
            timer_feedback = self._start_step_timer()
            if timer_feedback:
                return self._result(COOKING, True, timer_feedback, current_step=self.step_index + 1)
        if self._signals_step_timer_start(text):
            timer_feedback = self._start_step_timer()
            if timer_feedback:
                return self._result(COOKING, True, timer_feedback, current_step=self.step_index + 1)
        if self._is_step_acknowledgment(text):
            return self._result(COOKING, True, feedback(
                self.phrases.choose("waiting_ack", WAITING_ACKNOWLEDGMENTS),
                "继续当前步骤｜完成后告诉我",
                robot_action="nod", led_effect="warm_white", expression="focused",
            ), current_step=self.step_index + 1)
        if self._acknowledges_step_completion(text):
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
        service = self.offline_question_service if self._provider_mode() in {"mock", "local_cache"} else self.question_service
        answer = service.answer(text, CookingContext(
            recipe=self.current_recipe, current_step=self._step(), servings=self.request.servings,
            taste_preferences=self.request.taste_preferences, dietary_restrictions=self.request.dietary_restrictions,
            available_ingredients=self.request.available_ingredients, available_equipment=self.request.available_equipment,
            timer_remaining_seconds=self._remaining_seconds(), conversation_summary=self.conversation_summary[-5:],
        ))
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

    def _handle_timer(self, text: str) -> dict[str, Any] | None:
        if "取消计时" in text:
            self.timer = None
            return self._result(self.state, True, feedback("计时已取消。", "计时已取消", robot_action="stop", led_effect="white", expression="neutral"))
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
            if timer.step_index is not None and timer.step_index == self.step_index and self.current_recipe:
                step = self._step()
                if "正面" in timer.label or ("牛排" in self.current_recipe.get("name", "") and "翻面" not in step["instruction"]):
                    self.awaiting_flip_confirmation = True
                    return self._result(COOKING, True, feedback(
                        "正面计时到了，请安全翻面；翻好后告诉我“翻面好了”。",
                        "正面完成｜请翻面",
                        robot_action="show_concern", led_effect="yellow", expression="alert",
                    ), current_step=self.step_index + 1)
                if "另一面" in timer.label:
                    if self.step_index < len(self.current_recipe["steps"]) - 1:
                        self.step_index += 1
                    return self._result(COOKING, True,
                        feedback("另一面计时到了，请把牛排移到盘中静置。", "另一面完成｜开始静置", robot_action="nod", led_effect="green_dynamic", expression="focused"),
                        self._current_step_feedback(), current_step=self.step_index + 1,
                    )
            return self._result(self.state, True, feedback("计时结束，请检查当前烹饪步骤。", "计时结束", robot_action="wave_hand", led_effect="yellow", expression="alert"))
        return None

    def _start_step_timer(self, label: str | None = None) -> dict[str, str] | None:
        step = self._step()
        seconds = step.get("duration_seconds")
        if not isinstance(seconds, (int, float)) or seconds <= 0:
            return None
        seconds = int(seconds)
        if label is None:
            if self.current_recipe and "牛排" in self.current_recipe.get("name", ""):
                label = "另一面" if "翻面" in step["instruction"] or "另一面" in step["instruction"] else "正面"
            else:
                label = f"第 {self.step_index + 1} 步"
        self.timer = Timer(self.clock() + seconds, seconds, label=label, step_index=self.step_index)
        return feedback(
            f"好的，{label}开始计时 {self._format_seconds(seconds)}。我会在时间到时提醒你。",
            f"{label}倒计时：{self._format_seconds(seconds)}",
            robot_action="nod", led_effect="green_dynamic", expression="focused",
        )

    def _current_step_feedback(self, prefix: str = "") -> dict[str, str]:
        step = self._step()
        instruction = step["instruction"]
        timer_hint = self._step_timer_hint(step)
        if timer_hint:
            instruction = f"{instruction} {timer_hint}"
        if step.get("safety_note"):
            instruction += f" 注意：{step['safety_note']}"
        display = step["display_text"]
        if timer_hint:
            display = f"{display}\n{timer_hint}"
        return feedback(f"{prefix}{instruction}", display, robot_action=step["robot_action"], led_effect=step["led_effect"], expression=step["expression"])

    def _step_timer_hint(self, step: dict[str, Any]) -> str | None:
        seconds = step.get("duration_seconds")
        if not isinstance(seconds, (int, float)) or seconds <= 0:
            return None
        if not any(word in str(step.get("instruction", "")) for word in ("煮", "炖", "焖", "煎", "烤", "蒸", "炸", "焯", "炒", "收汁", "加热")):
            return None
        return f"准备计时 {self._format_seconds(int(seconds))}；食材下锅后说“下锅了”或“开始炒”，我就开始计时。"

    @staticmethod
    def _signals_step_timer_start(text: str) -> bool:
        compact = text.replace(" ", "")
        if any(marker in compact for marker in ("吗", "？", "?")):
            return False
        has_action = any(word in compact for word in ("下锅", "倒入", "放入", "开始翻炒"))
        has_completion = any(marker in compact for marker in ("已经", "了", "啦", "好了", "开始"))
        return has_action and has_completion

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
        compact = text.replace(" ", "").lower()
        if any(word in compact for word in ("吗", "？", "?", "怎么", "如何", "什么时候", "要不要")):
            return False
        completion_markers = (
            "切好了", "切完了", "洗好了", "洗完了", "打散了", "搅好了",
            "倒好了", "放好了", "加好了", "炒好了", "炒完了", "煮好了",
            "煮完了", "焯好了", "完成了", "完成啦", "弄好了", "弄完了", "做好了",
            "搞定了", "ok了", "ok啦", "ok", "好了",
            "烧开了", "煮开了", "水开了", "沸腾了",
        )
        return any(marker in compact for marker in completion_markers)

    def _advance_after_completion(self) -> dict[str, Any]:
        assert self.current_recipe is not None
        completed_number = self.step_index + 1
        if self.step_index >= len(self.current_recipe["steps"]) - 1:
            self.state = COMPLETED
            return self._result(COMPLETED, False, self._finished_feedback(), current_recipe=self._recipe_metadata())
        self.step_index += 1
        encouragement = self.phrases.choose("step_encouragement", STEP_ENCOURAGEMENTS)
        return self._result(
            COOKING,
            True,
            feedback(
                f"{encouragement} 已完成第 {completed_number} 步。接下来进行第 {self.step_index + 1} 步。",
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
        self.conversation_summary = []
        self.awaiting_flip_confirmation = False
        self._active_provider = self.provider
        self._used_provider_fallback = False

    @staticmethod
    def _has(text: str, *phrases: str) -> bool:
        return any(phrase in text for phrase in phrases)

    @staticmethod
    def _format_seconds(seconds: int) -> str:
        return f"{seconds // 60} 分钟" if seconds >= 60 and seconds % 60 == 0 else f"{seconds} 秒"

    def _is_steak_request(self) -> bool:
        return "牛排" in str(self.request.requested_dish or "")

    def _ensure_recipe_respects_restrictions(self) -> bool:
        if not self.current_recipe:
            return False
        names = [str(item.get("name", "")) for item in self.current_recipe.get("ingredients", [])]
        return not ingredient_conflicts(names, self.request.dietary_restrictions)

    def _raw_meat_names(self) -> list[str]:
        if not self.current_recipe:
            return []
        meat_words = ("牛排", "肥牛", "牛肉", "猪肉", "排骨", "肉丝", "鸡肉", "鸡翅", "羊肉", "鱼", "虾")
        found: list[str] = []
        for item in self.current_recipe.get("ingredients", []):
            name = str(item.get("name", ""))
            if any(word in name for word in meat_words) and name not in found:
                found.append(name)
        return found

    def _new_cache_filename(self) -> str | None:
        paths = getattr(self._active_provider, "last_cache_paths", None)
        if isinstance(paths, list) and len(paths) > 1:
            return f"{len(paths)} 个候选"
        path = getattr(self._active_provider, "last_cache_path", None)
        return path.name if isinstance(path, Path) else None

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
