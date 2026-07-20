from __future__ import annotations

import sys
from pathlib import Path

import pytest

from runtime_core.executor import RuntimeExecutor
from runtime_core.skill_manager import SkillManager


ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = ROOT / "skills" / "kitchen_assistant"
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

from kitchen.cooking_question_service import RuleBasedCookingQuestionService  # noqa: E402
from kitchen.dish_profiles import load_catalog  # noqa: E402
from kitchen.ingredient_vocabulary import (  # noqa: E402
    canonicalize_ingredient, extract_known_ingredients, ingredient_present,
    is_animal_protein, is_seasoning, restriction_matches,
)
from kitchen.models import CookingAnswer, CookingContext, RecipeCandidate, RecipeSearchRequest  # noqa: E402
from kitchen.recipe_normalizer import RecipeNormalizationError, RecipeNormalizer  # noqa: E402
from kitchen.response_phrases import (  # noqa: E402
    FINISHED_RESPONSES, SINGLE_DINER_COMPANIONS, STEP_ENCOURAGEMENTS,
)
from kitchen.request_parser import parse_updates  # noqa: E402
from kitchen.session_store import KitchenSession  # noqa: E402
from kitchen.states import (  # noqa: E402
    CANCELLED, COLLECTING_INGREDIENTS, COLLECTING_PREFERENCES, COMPLETED, COOKING, PAUSED,
    PRESENTING_CANDIDATES, WAITING_MEAT_THAW, WAITING_RECIPE_CONFIRMATION,
)
from providers.mock_recipe_provider import MockRecipeSearchProvider  # noqa: E402
from providers.online_recipe_provider import OnlineRecipeSearchProvider  # noqa: E402


def items(response: dict) -> list[dict]:
    return response.get("steps", [response])


def assert_multimodal(response: dict) -> None:
    for item in items(response):
        assert item.get("speech") or item.get("question")
        for key in ("display", "robot_action", "led_effect", "expression"):
            assert item.get(key), key


def test_shared_ingredient_vocabulary_covers_daily_cooking_without_false_spicy_matches() -> None:
    assert ingredient_present("蘑菇", ["杏鲍菇"])
    assert ingredient_present("牛肉", ["牛排"])
    assert ingredient_present("豆腐", ["嫩豆腐"])
    assert ingredient_present("面条", ["挂面"])
    assert ingredient_present("食用油", ["菜籽油"])
    assert ingredient_present("玉米", ["玉米粒"])
    assert restriction_matches("海鲜", "虾仁")
    assert restriction_matches("辣", "小米辣")
    assert not restriction_matches("辣", "青椒")
    assert restriction_matches("乳制品", "芝士")
    assert restriction_matches("花生", "花生油")
    assert restriction_matches("麸质", "面条")
    assert is_seasoning("菜籽油")
    assert is_seasoning("蒸鱼豉油")
    assert is_animal_protein("鸡腿肉")
    assert not is_animal_protein("鸡精")
    assert not is_animal_protein("鸡蛋")
    assert canonicalize_ingredient("洋芋") == "土豆"
    assert extract_known_ingredients("家里有洋芋、花菜和鸡腿肉") == ["土豆", "菜花", "鸡腿肉"]


def make_session(*, clock=lambda: 100.0, provider=None) -> KitchenSession:
    return KitchenSession(SKILL_ROOT / "recipes" / "recipes.json", clock=clock, recipe_provider=provider)


def start_known_dish(session: KitchenSession, dish: str = "番茄鸡蛋面") -> dict:
    response = session.handle(f"我想做{dish}")
    assert response["kitchen_state"] == COLLECTING_PREFERENCES
    response = session.handle("一个人吃")
    assert response["kitchen_state"] == COLLECTING_PREFERENCES
    response = session.handle("少盐")
    assert response["kitchen_state"] == PRESENTING_CANDIDATES
    return response


def choose_and_confirm(session: KitchenSession, choice: str = "第一个") -> dict:
    chosen = session.handle(choice)
    assert chosen["kitchen_state"] == WAITING_RECIPE_CONFIRMATION
    assert session.current_recipe is None
    cooking = session.handle("开始吧")
    assert cooking["kitchen_state"] == COOKING
    assert session.current_recipe is not None
    return cooking


def test_recipes_catalog_owns_dish_workflow_profiles() -> None:
    recipes, profiles = load_catalog(SKILL_ROOT / "recipes")

    assert recipes and "steak" in profiles and "ribs" in profiles
    assert profiles["steak"]["preference_questions"][0]["field"] == "steak_doneness"
    assert profiles["steak"]["timer_steps"][0]["timer_end_action"] == "await_confirmation"
    assert profiles["ribs"]["ensure_predecessor"]["waiting_marker"] == "腌制"


def test_hello_and_active_kitchen_short_commands_route_correctly() -> None:
    manager = SkillManager()
    manager.load_skills()
    assert manager.run_user_text("你好")["selected_skill"] == "hello_skill"
    started = manager.run_user_text("厨房助手你好")
    assert started["selected_skill"] == "kitchen_assistant"
    started = manager.run_user_text("我想做番茄鸡蛋面")
    assert started["selected_skill"] == "kitchen_assistant"
    assert manager.run_user_text("一个人")["selected_skill"] == "kitchen_assistant"
    assert manager.run_user_text("少盐")["kitchen_state"] == PRESENTING_CANDIDATES
    assert manager.run_user_text("第一个")["kitchen_state"] == WAITING_RECIPE_CONFIRMATION
    assert manager.run_user_text("可以")["kitchen_state"] == COOKING
    assert manager.run_user_text("下一步")["selected_skill"] == "kitchen_assistant"
    ended = manager.run_user_text("退出厨房助手")
    assert ended["kitchen_state"] == CANCELLED
    assert manager.active_skill_name is None


def test_pantry_recommendation_sentence_routes_to_kitchen_assistant() -> None:
    manager = SkillManager()
    manager.load_skills()
    response = manager.run_user_text("牛肉和蘑菇不知道做什么菜")

    assert response["selected_skill"] == "kitchen_assistant"
    assert response["kitchen_state"] == COLLECTING_PREFERENCES


def test_ai_recipe_search_emits_immediate_waiting_progress() -> None:
    session = make_session()
    session.provider.supports_ai = True
    progress: list[dict] = []
    session.set_progress_callback(progress.append)

    session.handle("我想做番茄鸡蛋面")
    session.handle("一个人")
    response = session.handle("少盐")

    assert progress[0]["display"] == "请稍后，正在为你查找菜谱"
    assert all("正在为你生成菜谱" not in item.get("display", "") for item in items(response))


def test_unknown_agent_question_emits_thinking_progress() -> None:
    class Client:
        @staticmethod
        def is_available() -> bool:
            return True

    class Agent:
        llm_client = Client()

        @staticmethod
        def answer(question, context):
            return CookingAnswer("可以这样处理。", "可以这样处理。")

    session = make_session()
    session.question_service = Agent()
    session.current_recipe = RecipeNormalizer().normalize({
        "name": "测试菜",
        "ingredients": [{"name": "土豆", "amount": 200, "unit": "克"}],
        "steps": [{"instruction": "把土豆切块"}],
    })
    session.state = COOKING
    progress: list[dict] = []
    session.set_progress_callback(progress.append)

    response = session.handle("这个做法为什么要这样安排")

    assert progress[0]["display"] == "请稍后，我正在思考要怎么应对"
    assert response["speech"].endswith("可以这样处理。")


def test_free_form_requested_dish_keeps_full_name_and_generic_cooking_phrase_stays_generic() -> None:
    assert parse_updates("我要做番茄肥牛").requested_dish == "番茄肥牛"
    assert parse_updates("我想吃油焖鸡").requested_dish == "油焖鸡"
    assert parse_updates("我要吃油焖鸡").requested_dish == "油焖鸡"
    assert parse_updates("我想好要做什么了，要做番茄肥牛").requested_dish == "番茄肥牛"
    assert parse_updates("红烧排骨怎么做").requested_dish == "红烧排骨"
    assert parse_updates("香菇滑鸡怎么做").requested_dish == "香菇滑鸡"
    assert parse_updates("我要做饭了").requested_dish is None
    assert parse_updates("我想吃点什么").requested_dish is None


@pytest.mark.parametrize("text", ["我想吃油焖鸡", "我要吃油焖鸡"])
def test_natural_want_to_eat_phrases_route_to_kitchen(text: str) -> None:
    manager = SkillManager()
    manager.load_skills()

    response = manager.run_user_text(text)

    assert response["selected_skill"] == "kitchen_assistant"
    assert response["kitchen_state"] == COLLECTING_PREFERENCES
    assert response["_route"]["matched_evidence"]


def test_explicit_dish_request_retries_an_empty_candidate_search() -> None:
    class EmptyThenCandidateProvider:
        mode = "mock"

        def __init__(self) -> None:
            self.requests: list[str | None] = []

        def search_recipes(self, request):
            self.requests.append(request.requested_dish)
            if len(self.requests) == 1:
                return []
            return [RecipeCandidate(
                candidate_id="oil_braised_chicken",
                title="油焖鸡",
                source_name="测试菜谱",
                source_url=None,
                summary="家常油焖鸡。",
                estimated_minutes=30,
                difficulty="简单",
                main_ingredients=["鸡肉"],
                missing_ingredients=[],
                match_reason="指定菜名。",
            )]

    provider = EmptyThenCandidateProvider()
    session = make_session(provider=provider)
    session.handle("我要做油焖鸡")
    session.handle("一个人")
    empty = session.handle("正常")
    assert empty["recipe_candidates"] == []

    retried = session.handle("我想吃油焖鸡")

    assert provider.requests == ["油焖鸡", "油焖鸡"]
    assert retried["recipe_candidates"][0]["title"] == "油焖鸡"


def test_food_how_to_questions_route_to_kitchen_without_capturing_generic_how_to() -> None:
    manager = SkillManager()
    manager.load_skills()
    assert manager.run_user_text("红烧排骨怎么做")['selected_skill'] == "kitchen_assistant"
    assert manager.run_user_text("香菇滑鸡怎么做")['selected_skill'] == "kitchen_assistant"
    fresh_manager = SkillManager()
    fresh_manager.load_skills()
    assert fresh_manager.run_user_text("Python 程序怎么做")['route'] == "normal_chat"


def test_inventory_parser_keeps_explicit_pantry_separate_from_requested_dish() -> None:
    updates = parse_updates("我有牛排、橄榄油、黄油、盐和黑胡椒，想做煎牛排")
    assert updates.requested_dish == "煎牛排"
    assert {"牛排", "橄榄油", "黄油", "盐", "黑胡椒"} <= set(updates.ingredients)


def test_common_allergy_and_omission_phrases_become_restrictions() -> None:
    updates = parse_updates("我对鸡蛋过敏，不要香菜，也不吃牛肉")
    assert {"鸡蛋", "香菜", "牛肉"} <= set(updates.restrictions)


@pytest.mark.parametrize("text", ["1个人", "一 个 人", "2个人", "三个人"])
def test_serving_parser_accepts_common_person_count_phrases(text: str) -> None:
    assert parse_updates(text).servings in {1, 2, 3}


def test_bare_number_is_accepted_only_when_session_asks_for_servings() -> None:
    session = make_session()
    session.handle("我想做番茄鸡蛋面")
    response = session.handle("1")
    assert session.servings == 1
    assert "口味" in response["question"]


def test_single_serving_gets_companion_feedback() -> None:
    session = make_session()
    session.handle("我想做番茄鸡蛋面")
    response = session.handle("1个人")
    assert "一个人吃饭" in response["question"]
    assert response["led_effect"] == "warm_white"
    assert response["robot_action"] == "encourage_gesture"


def test_known_dish_requires_explicit_confirmation_before_cooking() -> None:
    session = make_session()
    response = start_known_dish(session)
    assert response["recipe_candidates"][0]["title"] == "番茄鸡蛋面"
    chosen = session.handle("第一个")
    assert_multimodal(chosen)
    assert session.current_recipe is None
    waiting = session.handle("我再想想")
    assert waiting["kitchen_state"] == WAITING_RECIPE_CONFIRMATION
    cooking = session.handle("按这个做")
    assert cooking["kitchen_state"] == COOKING
    assert_multimodal(cooking)


@pytest.mark.parametrize("confirmation", ["好", "好的"])
def test_natural_affirmation_confirms_recipe_without_redundant_prompt(confirmation: str) -> None:
    session = make_session()
    start_known_dish(session, "番茄炒蛋")
    session.handle("第一个")
    response = session.handle(confirmation)
    assert response["kitchen_state"] == COOKING
    assert "请明确说" not in str(response)


@pytest.mark.parametrize("confirmation", ["好", "好的", "好呀", "可以", "开始吧"])
def test_single_candidate_accepts_natural_confirmation_without_ordinal(
    confirmation: str,
) -> None:
    session = make_session()
    candidates = start_known_dish(session, "番茄炒蛋")
    assert len(candidates["recipe_candidates"]) == 1
    candidate_prompt = str(candidates)
    assert "第一个" not in candidate_prompt
    assert "第二个" not in candidate_prompt
    assert "好" in candidate_prompt or "开始吧" in candidate_prompt

    response = session.handle(confirmation)

    assert response["kitchen_state"] == COOKING
    assert session.selected_candidate == session.recipe_candidates[0]
    rendered = "\n".join(
        str(step.get(key, ""))
        for step in response.get("steps", [])
        for key in ("speech", "question", "display")
    )
    assert "第一个" not in rendered
    assert "第二个" not in rendered


def test_multiple_candidates_do_not_guess_which_recipe_good_refers_to() -> None:
    class TwoCandidateProvider:
        mode = "mock"

        def search_recipes(self, request):
            return [
                RecipeCandidate(
                    candidate_id=f"candidate-{index}",
                    title=f"测试菜{index}",
                    source_name="测试",
                    source_url=None,
                    summary="测试候选",
                    estimated_minutes=10,
                    difficulty="简单",
                    main_ingredients=["番茄"],
                    missing_ingredients=[],
                    match_reason="测试",
                )
                for index in (1, 2)
            ]

    session = make_session(provider=TwoCandidateProvider())
    session.handle("我想做测试菜")
    session.handle("一个人")
    candidates = session.handle("正常")
    assert len(candidates["recipe_candidates"]) == 2

    response = session.handle("好")

    assert response["kitchen_state"] == PRESENTING_CANDIDATES
    assert session.selected_candidate is None


def test_plain_start_confirms_recipe() -> None:
    session = make_session()
    start_known_dish(session, "番茄炒蛋")
    session.handle("第一个")
    response = session.handle("开始")
    assert response["kitchen_state"] == COOKING
    assert session.current_recipe is not None


def test_bare_candidate_number_selects_the_requested_item() -> None:
    session = make_session()
    session.handle("我不知道晚饭做什么")
    session.handle("我有鸡蛋、番茄和面条")
    response = session.handle("一个人，少盐")
    assert response["kitchen_state"] == PRESENTING_CANDIDATES
    assert len(session.recipe_candidates) == 1
    response = session.handle("1")
    assert response["kitchen_state"] == WAITING_RECIPE_CONFIRMATION
    assert session.selected_candidate == session.recipe_candidates[0]


def test_unknown_inventory_is_not_displayed_as_missing_ingredients() -> None:
    session = make_session()
    candidates = start_known_dish(session, "番茄鸡蛋面")
    assert "缺：" not in candidates["steps"][-1]["display"]
    selected = session.handle("第一个")
    assert "食材库存：未提供" in selected["display"]


def test_offline_requested_dish_matches_exactly_or_returns_no_candidates() -> None:
    session = make_session()
    candidates = start_known_dish(session, "红烧排骨")
    assert [item["title"] for item in candidates["recipe_candidates"]] == ["红烧排骨"]

    session = make_session()
    response = start_known_dish(session, "鱼香肉丝")
    assert response["recipe_candidates"] == []
    assert "不会用无关菜谱替代" in response["steps"][-1]["speech"]


def test_unknown_dinner_flow_parses_ingredients_and_ranks_candidates() -> None:
    session = make_session()
    response = session.handle("我不知道晚饭做什么")
    assert response["kitchen_state"] == COLLECTING_INGREDIENTS
    assert_multimodal(response)
    response = session.handle("我有鸡蛋、番茄和面条")
    assert session.request.available_ingredients == ["鸡蛋", "番茄", "面条"]
    assert response["kitchen_state"] == COLLECTING_PREFERENCES
    response = session.handle("一个人，少盐")
    assert response["kitchen_state"] == PRESENTING_CANDIDATES
    candidates = response["recipe_candidates"]
    assert len(candidates) <= 3
    assert candidates[0]["title"] == "番茄鸡蛋面"
    assert candidates[0]["missing_ingredients"] == []
    chosen = session.handle("第一个")
    assert chosen["kitchen_state"] == WAITING_RECIPE_CONFIRMATION


def test_unknown_dinner_with_ingredients_in_first_sentence_skips_repeat_inventory_question() -> None:
    session = make_session()
    response = session.handle("牛肉和蘑菇不知道做什么菜")

    assert session.request.available_ingredients == ["牛肉", "蘑菇"]
    assert response["kitchen_state"] == COLLECTING_PREFERENCES
    assert "几个人" in response["steps"][-1].get("question", "")


def test_only_available_ingredients_wording_immediately_starts_recommendation_flow() -> None:
    session = make_session()
    response = session.handle("我要做饭，但我现在只有蘑菇和牛肉，要怎么做比较好呢？")

    assert session.request.requested_dish is None
    assert session.request.available_ingredients == ["牛肉", "蘑菇"]
    assert response["kitchen_state"] == COLLECTING_PREFERENCES
    assert "几个人" in response["steps"][-1].get("question", "")


def test_offline_recommendations_do_not_drop_explicit_pantry_ingredients() -> None:
    session = make_session()
    session.handle("牛肉和蘑菇不知道做什么")
    session.handle("一个人")
    response = session.handle("正常")

    assert response["recipe_candidates"] == []
    assert "不会忽略其中任何一种" in response["steps"][-1]["speech"]


def test_candidates_support_refresh_and_simple_fast_filters() -> None:
    session = make_session()
    start_known_dish(session, "咖喱饭")
    first_ids = [candidate.candidate_id for candidate in session.recipe_candidates]
    session.handle("换一批")
    assert session.state == PRESENTING_CANDIDATES
    assert all(candidate.candidate_id not in first_ids for candidate in session.recipe_candidates) or not session.recipe_candidates
    session = make_session()
    start_known_dish(session, "咖喱饭")
    session.handle("第一个")
    filtered = session.handle("换一个更简单的")
    assert filtered["kitchen_state"] == PRESENTING_CANDIDATES
    assert session.request.difficulty_preference == "简单"


def test_combined_top_candidate_selection_and_confirmation_still_emits_overview() -> None:
    session = make_session()
    start_known_dish(session)
    response = session.handle("就这个，开始吧")
    assert response["kitchen_state"] == COOKING
    assert any("来源：" in item.get("display", "") for item in items(response))


def test_dynamic_recipe_steps_and_legacy_tomato_egg_fallback_work() -> None:
    session = make_session()
    start_known_dish(session, "番茄炒蛋")
    cooking = choose_and_confirm(session)
    assert session.current_recipe["name"] == "番茄炒蛋"
    assert_multimodal(cooking)
    original = session.step_index
    repeated = session.handle("再说一遍")
    assert session.step_index == original
    session.handle("下一步")
    assert session.step_index == original + 1
    back = session.handle("上一步")
    assert session.step_index == original
    assert_multimodal(back)


def test_explicit_step_completion_advances_context_but_questions_do_not() -> None:
    session = make_session()
    start_known_dish(session, "番茄炒蛋")
    choose_and_confirm(session)
    original = session.step_index
    question = session.handle("番茄切好了吗？")
    assert session.step_index == original
    assert question["kitchen_state"] == COOKING
    completed = session.handle("番茄已经洗好并切好了")
    assert session.step_index == original + 1
    assert completed["current_step"] == original + 2
    assert "已完成第 1 步" in completed["steps"][0]["speech"]


def test_boiling_completion_advances_but_boiling_question_does_not() -> None:
    session = make_session()
    session.current_recipe = RecipeNormalizer().normalize({
        "name": "测试汤", "ingredients": [{"name": "水", "amount": 500, "unit": "毫升"}],
        "steps": [{"instruction": "锅中加水烧开。"}, {"instruction": "放入食材煮熟。"}],
    })
    session.state = COOKING
    question = session.handle("水烧开了吗？")
    assert session.step_index == 0
    assert "持续冒出" in question["speech"]
    completed = session.handle("水烧开了")
    assert session.step_index == 1
    assert "已完成第 1 步" in completed["steps"][0]["speech"]


def test_ok_completion_phrase_advances_with_encouragement_feedback() -> None:
    session = make_session()
    start_known_dish(session, "番茄炒蛋")
    choose_and_confirm(session)
    response = session.handle("OK了")
    assert session.step_index == 1
    assert response["steps"][0]["led_effect"] == "green_dynamic"
    assert "已完成第 1 步" in response["steps"][0]["speech"]


def test_plain_good_acknowledges_without_advancing_but_explicit_done_advances() -> None:
    session = make_session()
    start_known_dish(session, "番茄炒蛋")
    choose_and_confirm(session)
    before = session.step_index
    response = session.handle("好的")
    assert session.step_index == before
    assert response["current_step"] == before + 1
    completed = session.handle("做好了")
    assert session.step_index == before + 1
    assert completed["current_step"] == before + 2
    before = session.step_index
    thanks = session.handle("谢谢")
    assert session.step_index == before
    assert "不客气" in thanks["speech"]
    assert thanks["led_effect"] == "warm_white"


def test_gratitude_and_waiting_phrases_rotate_without_changing_state() -> None:
    session = make_session()
    start_known_dish(session, "番茄炒蛋")
    choose_and_confirm(session)
    before = session.step_index
    first_thanks = session.handle("谢谢")
    second_thanks = session.handle("谢谢")
    assert first_thanks["speech"] != second_thanks["speech"]
    assert session.step_index == before
    first_ack = session.handle("好的")
    second_ack = session.handle("好的")
    assert first_ack["speech"] != second_ack["speech"]
    assert session.step_index == before


def test_pause_resume_timer_and_completion_remain_available() -> None:
    now = [100.0]
    session = make_session(clock=lambda: now[0])
    start_known_dish(session)
    choose_and_confirm(session)
    session.handle("帮我计时五秒")
    now[0] = 102.0
    assert "3 秒" in session.handle("还有多久")["speech"]
    paused = session.handle("暂停一下")
    assert paused["kitchen_state"] == PAUSED
    assert session.handle("继续")["kitchen_state"] == COOKING
    now[0] = 106.0
    assert "计时结束" in session.handle("计时结束了吗")["display"]
    assert "取消" in session.handle("取消计时")["speech"]


def test_heat_step_announces_timer_and_starts_when_food_hits_pan() -> None:
    now = [100.0]
    session = make_session(clock=lambda: now[0])
    session.current_recipe = RecipeNormalizer().normalize({
        "name": "宫保鸡丁",
        "ingredients": [{"name": "鸡肉丁", "amount": 150, "unit": "克"}],
        "steps": [{
            "instruction": "倒入腌制好的鸡肉丁，中火快速翻炒至鸡肉丁完全变色",
            "duration_seconds": 120,
            "heat_level": "中火",
        }],
    })
    session.state = COOKING
    prompt = session.handle("再说一遍")
    assert "准备计时 2 分钟" in prompt["speech"]
    assert "下锅了" in prompt["speech"]
    assert session.timer is None

    started = session.handle("鸡肉丁下锅了")
    assert "开始计时 2 分钟" in started["speech"]
    assert session.timer is not None


def test_timed_step_cannot_be_skipped_before_timer_starts() -> None:
    session = make_session(clock=lambda: 100.0)
    session.current_recipe = RecipeNormalizer().normalize({
        "name": "计时保护测试",
        "ingredients": [{"name": "鸡肉", "amount": 150, "unit": "克"}],
        "steps": [
            {"instruction": "放入鸡肉翻炒至完全变色", "duration_seconds": 120},
            {"instruction": "关火装盘"},
        ],
    })
    session.state = COOKING

    blocked = session.handle("下一步")
    assert blocked["current_step"] == 1
    assert "计时还没有启动" in blocked["speech"]
    assert session.step_index == 0

    started = session.handle("开始计时")
    assert "开始计时 2 分钟" in started["speech"]
    assert session.timer is not None


def test_timed_step_allows_explicit_external_timer_confirmation() -> None:
    session = make_session(clock=lambda: 100.0)
    session.current_recipe = RecipeNormalizer().normalize({
        "name": "外部计时确认测试",
        "ingredients": [{"name": "蘑菇", "amount": 100, "unit": "克"}],
        "steps": [
            {"instruction": "放入蘑菇煮软", "duration_seconds": 120},
            {"instruction": "关火装盘"},
        ],
    })
    session.state = COOKING

    session.handle("做好了")
    advanced = session.handle("确认完成")

    assert advanced["current_step"] == 2
    assert session.step_index == 1


def test_preheat_step_keeps_explicit_seconds_timer() -> None:
    recipe = RecipeNormalizer().normalize({
        "name": "预热计时测试",
        "ingredients": [{"name": "食用油", "amount": 10, "unit": "毫升"}],
        "steps": [{"instruction": "中火预热空锅约30秒"}],
    })

    step = recipe["steps"][0]
    assert step["duration_seconds"] == 30
    session = make_session(clock=lambda: 100.0)
    session.current_recipe = recipe
    session.state = COOKING
    assert "准备计时 30 秒" in session.handle("再说一遍")["speech"]


def test_normalizer_adds_missing_meat_timer_and_rebuilds_full_display() -> None:
    recipe = RecipeNormalizer().normalize({
        "name": "宫保鸡丁",
        "ingredients": [{"name": "鸡肉丁", "amount": 150, "unit": "克"}],
        "steps": [{
            "instruction": "倒入腌制好的鸡肉丁，中火快速翻炒至鸡肉丁完全变色",
            "duration_seconds": None,
            "display_text": "步骤 1/1：倒入腌制好的鸡肉丁",
            "safety_note": "计时只是下限参考，应以肉丝完全变色、中心无粉红为准。",
        }],
    })
    step = recipe["steps"][0]
    assert step["duration_seconds"] == 120
    assert "肉类完全变色" in step["safety_note"]
    assert "鸡肉丁完全变色" in step["display_text"]


def test_candidate_separates_main_foods_from_main_seasonings() -> None:
    provider = MockRecipeSearchProvider(SKILL_ROOT / "recipes")
    candidate = provider.search_recipes(RecipeSearchRequest(requested_dish="番茄鸡蛋面"))[0]

    assert candidate.main_ingredients == ["番茄", "鸡蛋", "面条"]
    assert candidate.main_seasonings == ["盐"]


def test_normalizer_scales_local_ingredients_to_requested_servings() -> None:
    recipe = RecipeNormalizer().normalize({
        "name": "两人份测试菜",
        "default_servings": 2,
        "ingredients": [
            {"name": "牛肉", "amount": 300, "unit": "克"},
            {"name": "生抽", "amount": "2", "unit": "汤匙"},
            {"name": "盐", "amount": "1/2", "unit": "茶匙"},
        ],
        "steps": [{"instruction": "将300克牛肉加入2汤匙生抽和1/2茶匙盐抓匀"}],
    }, servings=1)

    assert recipe["servings"] == 1
    assert [(item["name"], item["amount"]) for item in recipe["ingredients"]] == [
        ("牛肉", "150"), ("生抽", "1"), ("盐", "1/4"),
    ]
    assert "150克牛肉" in recipe["steps"][0]["instruction"]
    assert "1汤匙生抽" in recipe["steps"][0]["instruction"]
    assert "1/4茶匙盐" in recipe["steps"][0]["instruction"]


def test_unmarked_local_recipe_uses_one_serving_as_legacy_baseline() -> None:
    recipe = RecipeNormalizer().normalize({
        "name": "旧版单人菜谱",
        "ingredients": [
            {"name": "牛肉", "amount": "150 克"},
            {"name": "生抽", "amount": "1 汤匙"},
            {"name": "盐", "amount": "1/4 茶匙"},
        ],
        "steps": [{"instruction": "把150克牛肉加入1汤匙生抽和1/4茶匙盐抓匀"}],
    }, servings=2)

    assert recipe["servings"] == 2
    assert [(item["name"], item["amount"]) for item in recipe["ingredients"]] == [
        ("牛肉", "300 克"), ("生抽", "2 汤匙"), ("盐", "1/2 茶匙"),
    ]
    assert "300克牛肉" in recipe["steps"][0]["instruction"]
    assert "2汤匙生抽" in recipe["steps"][0]["instruction"]
    assert "1/2茶匙盐" in recipe["steps"][0]["instruction"]


def test_selected_candidate_displays_main_seasonings() -> None:
    session = make_session()
    start_known_dish(session)

    selected = session.handle("第一个")
    assert "主要食材：番茄、鸡蛋、面条" in selected["display"]
    assert "主要调味料：盐" in selected["display"]


def test_parallel_prep_is_confirmed_instead_of_repeated_later() -> None:
    session = make_session(clock=lambda: 100.0)
    session.current_recipe = RecipeNormalizer().normalize({
        "name": "测试腌制",
        "ingredients": [{"name": "鸡肉", "amount": 200, "unit": "克"}],
        "steps": [
            {"instruction": "鸡肉加料酒抓匀腌制10分钟"},
            {"instruction": "取小碗，倒入10毫升生抽、5毫升老抽和1克盐，搅匀成糖醋汁"},
            {"instruction": "热锅后倒油翻炒"},
        ],
    })
    session.state = COOKING
    session.handle("开始了")
    session.handle("做好了")
    check = session.handle("确认")
    assert "刚才计时时" in "".join(item.get("speech", "") for item in items(check))
    skipped = session.handle("对")
    assert skipped["current_step"] == 3
    assert "热锅后倒油" in "".join(item.get("speech", "") for item in items(skipped))


def test_parallel_hint_skips_vague_bowl_placeholder_and_names_measured_seasonings() -> None:
    session = make_session(clock=lambda: 100.0)
    session.current_recipe = RecipeNormalizer().normalize({
        "name": "糖醋排骨测试",
        "ingredients": [
            {"name": "排骨", "amount": 300, "unit": "克"},
            {"name": "生抽", "amount": 20, "unit": "毫升"},
            {"name": "米醋", "amount": 15, "unit": "毫升"},
            {"name": "白砂糖", "amount": 20, "unit": "克"},
        ],
        "steps": [
            {"instruction": "排骨加料酒抓匀腌制15分钟"},
            {"instruction": "腌制期间准备调料碗，腌制10分钟"},
            {"instruction": "取小碗，加入20毫升生抽、15毫升米醋、20克白砂糖，搅匀成糖醋汁"},
            {"instruction": "热锅倒油后放入排骨翻炒"},
        ],
    })
    session.state = COOKING
    session.step_index = next(
        index for index, step in enumerate(session.current_recipe["steps"])
        if "腌制15分钟" in step["instruction"]
    )

    started = session.handle("开始计时")
    speech = "".join(item.get("speech", "") for item in items(started))
    assert "20毫升生抽" in speech
    assert "15毫升米醋" in speech
    assert "20克白砂糖" in speech
    assert "准备调料碗，腌制10分钟" not in speech

    recorded = session.handle("糖醋汁搅匀了")
    assert "已记录你完成了" in recorded["speech"]


def test_marinade_parallel_prep_can_boiling_plain_water_but_never_selects_final_seasoning_step() -> None:
    session = make_session(clock=lambda: 100.0)
    session.current_recipe = RecipeNormalizer().normalize({
        "name": "蘑菇牛肉片汤",
        "ingredients": [{"name": "牛肉", "amount": 100, "unit": "克"}],
        "steps": [
            {"instruction": "牛肉片加入盐和生抽抓匀腌制10分钟"},
            {"instruction": "汤锅倒入500毫升清水，开大火加热至沸腾"},
            {"instruction": "水沸腾后放入蘑菇煮软"},
            {"instruction": "加入盐和生抽搅匀，关火后淋入香油，盛出即可"},
        ],
    })
    session.state = COOKING

    started = session.handle("开始计时")
    speech = "".join(item.get("speech", "") for item in items(started))
    assert "500毫升清水" in speech
    assert "只烧清水" in speech
    assert "关火后淋入香油" not in speech

    recorded = session.handle("水烧开了")
    assert "已记录你完成了" in recorded["speech"]


def test_marinade_without_independent_prep_only_offers_free_waiting() -> None:
    session = make_session(clock=lambda: 100.0)
    session.current_recipe = RecipeNormalizer().normalize({
        "name": "胡萝卜炖鸡肉",
        "ingredients": [
            {"name": "鸡肉", "amount": 150, "unit": "克"},
            {"name": "胡萝卜", "amount": 100, "unit": "克"},
            {"name": "盐", "amount": 2, "unit": "克"},
            {"name": "料酒", "amount": 15, "unit": "毫升"},
            {"name": "食用油", "amount": 10, "unit": "毫升"},
        ],
        "steps": [
            {"instruction": "把切好的150克鸡肉放入小碗，加入15毫升料酒、2克盐，抓匀后腌制10分钟"},
            {"instruction": "炒锅倒入10毫升食用油，开中火加热至油面微有波纹"},
            {"instruction": "放入腌制好的150克鸡肉块，翻炒至表面全部变色"},
        ],
    })
    session.state = COOKING

    started = session.handle("开始计时")
    speech = "".join(item.get("speech", "") for item in items(started))

    assert "在这段时间里你可以同步做自己想做的事情，时间到了我会叫你～" in speech
    assert "取一个小碗" not in speech
    assert "量好" not in speech
    assert session.parallel_offer_by_timer_step == {}


def test_parallel_candidate_does_not_repeat_marinade_ingredients() -> None:
    session = make_session(clock=lambda: 100.0)
    session.current_recipe = RecipeNormalizer().normalize({
        "name": "重复调料准备测试",
        "ingredients": [
            {"name": "鸡肉", "amount": 150, "unit": "克"},
            {"name": "盐", "amount": 2, "unit": "克"},
            {"name": "料酒", "amount": 15, "unit": "毫升"},
        ],
        "steps": [
            {"instruction": "鸡肉加入15毫升料酒和2克盐，抓匀后腌制10分钟"},
            {"instruction": "取一个小碗，量好2克盐和15毫升料酒，放在手边"},
            {"instruction": "炒锅加热后倒油，放入鸡肉炒熟"},
        ],
    })
    session.state = COOKING

    started = session.handle("开始计时")
    speech = "".join(item.get("speech", "") for item in items(started))

    assert "在这段时间里你可以同步做自己想做的事情，时间到了我会叫你～" in speech
    assert "量好2克盐" not in speech
    assert session.parallel_offer_by_timer_step == {}


def test_parallel_candidate_rejects_partial_reuse_of_marinade_seasoning() -> None:
    session = make_session(clock=lambda: 100.0)
    session.current_recipe = RecipeNormalizer().normalize({
        "name": "部分重复调料测试",
        "ingredients": [
            {"name": "鸡肉", "amount": 150, "unit": "克"},
            {"name": "盐", "amount": 2, "unit": "克"},
            {"name": "葱", "amount": 10, "unit": "克"},
        ],
        "steps": [
            {"instruction": "鸡肉加入2克盐抓匀后腌制10分钟"},
            {"instruction": "切好10克葱，再量取2克盐放入调料碗"},
            {"instruction": "炒锅加热后倒油，放入鸡肉炒熟"},
        ],
    })
    session.state = COOKING

    started = session.handle("开始计时")
    speech = "".join(item.get("speech", "") for item in items(started))

    assert "在这段时间里你可以同步做自己想做的事情，时间到了我会叫你～" in speech
    assert "切好10克葱" not in speech
    assert session.parallel_offer_by_timer_step == {}


def test_normalizer_does_not_split_one_marinade_into_two_timers() -> None:
    recipe = RecipeNormalizer().normalize({
        "name": "蘑菇牛肉片汤",
        "ingredients": [{"name": "牛肉", "amount": 100, "unit": "克"}],
        "steps": [
            {
                "instruction": "把切好的100克牛肉片放入小碗，加入1克盐、5毫升生抽抓匀，静置腌制，腌制10分钟",
                "duration_seconds": 600,
            },
            {"instruction": "汤锅倒入500毫升清水，开大火加热至沸腾"},
        ],
    })

    marinade_steps = [step for step in recipe["steps"] if "腌制" in step["instruction"]]
    assert len(marinade_steps) == 1
    assert marinade_steps[0]["duration_seconds"] == 600
    assert recipe["steps"][1]["instruction"].startswith("汤锅倒入500毫升清水")


def test_normalizer_repairs_cached_duplicate_bare_marinade_step() -> None:
    recipe = RecipeNormalizer().normalize({
        "name": "旧缓存腌制测试",
        "ingredients": [{"name": "牛肉", "amount": 100, "unit": "克"}],
        "steps": [
            {"instruction": "牛肉加入盐和生抽抓匀腌制10分钟", "duration_seconds": 600},
            {"instruction": "腌制10分钟", "duration_seconds": 600},
            {"instruction": "汤锅倒入500毫升清水，开大火加热至沸腾"},
        ],
    })

    assert [step["instruction"] for step in recipe["steps"]] == [
        "牛肉加入盐和生抽抓匀腌制10分钟",
        "汤锅倒入500毫升清水，开大火加热至沸腾",
    ]


def test_short_next_step_phrase_and_local_recipe_question_use_question_service() -> None:
    class Agent:
        def answer(self, question, context):
            return CookingAnswer("Agent 已回答。", "Agent 已回答。")

    session = make_session()
    session.question_service = Agent()
    session.current_recipe = RecipeNormalizer().normalize({
        "name": "测试菜",
        "ingredients": [{"name": "土豆", "amount": 200, "unit": "克"}],
        "steps": [{"instruction": "土豆切块"}, {"instruction": "加水煮熟"}],
    })
    session.state = COOKING
    assert session.handle("没有高压锅怎么办")["speech"].endswith("Agent 已回答。")
    assert session.handle("下步")["current_step"] == 2


def test_marinade_timer_supports_parallel_prep_and_natural_start_words() -> None:
    now = [100.0]
    session = make_session(clock=lambda: now[0])
    session.current_recipe = RecipeNormalizer().normalize({
        "name": "糖醋排骨",
        "ingredients": [{"name": "排骨", "amount": 200, "unit": "克"}],
        "steps": [
            {"instruction": "排骨加入料酒和姜片，抓匀腌制10分钟"},
            {"instruction": "准备白糖、生抽、米醋放在手边"},
        ],
    })
    session.state = COOKING
    session.step_index = 1  # 排骨菜会先自动补入焯水步骤。
    prompt = session.handle("再说一遍")
    assert prompt["current_step"] == 2
    assert "准备计时 10 分钟" in prompt["speech"]
    assert "等待期间可以先准备" not in prompt["speech"]
    started = session.handle("开始了")
    assert "开始计时 10 分钟" in started["speech"]
    assert "白糖" in started["speech"]
    assert session.timer is not None
    prep_done = session.handle("调料准备好了")
    assert prep_done["current_step"] == 2
    assert session.timer is not None
    confirm = session.handle("做好了")
    assert confirm["current_step"] == 2
    assert "确认结束计时" in confirm["display"]
    advanced = session.handle("确认")
    assert advanced["kitchen_state"] == COMPLETED
    assert session.timer is None


@pytest.mark.parametrize("utterance", ["我做好了", "下一步", "跳过这一步"])
def test_timed_step_requires_confirmation_before_early_advance(utterance: str) -> None:
    session = make_session(clock=lambda: 100.0)
    session.current_recipe = RecipeNormalizer().normalize({
        "name": "测试腌制",
        "ingredients": [{"name": "鸡肉", "amount": 200, "unit": "克"}],
        "steps": [
            {"instruction": "鸡肉加料酒抓匀腌制10分钟"},
            {"instruction": "量好生抽和盐"},
        ],
    })
    session.state = COOKING
    session.handle("开始计时")

    confirmation = session.handle(utterance)
    assert "确认结束计时" in confirmation["display"]
    assert session.step_index == 0
    session.handle("确认")
    assert session.step_index == 1
    assert session.timer is None


def test_rib_marinade_without_duration_is_timed_after_blanching_and_never_preheats_oil() -> None:
    now = [100.0]
    session = make_session(clock=lambda: now[0])
    session.current_recipe = RecipeNormalizer().normalize({
        "name": "糖醋排骨",
        "ingredients": [
            {"name": "排骨", "amount": 200, "unit": "克"},
            {"name": "生抽", "amount": 10, "unit": "毫升"},
            {"name": "老抽", "amount": 5, "unit": "毫升"},
            {"name": "盐", "amount": 1, "unit": "克"},
        ],
        "steps": [
            {"instruction": "排骨加入料酒和姜片，抓匀腌制"},
            {"instruction": "炒锅加食用油，放入冰糖小火加热至融化"},
        ],
    })
    session.state = COOKING
    assert "焯水" in session.current_recipe["steps"][0]["instruction"]
    assert "腌制10分钟" in session.current_recipe["steps"][1]["instruction"]
    assert session.current_recipe["steps"][1]["duration_seconds"] == 600
    session.step_index = 1
    prompt = session._current_step_feedback()
    assert "准备计时 10 分钟" in prompt["speech"]
    assert "炒锅加食用油" not in prompt["speech"]
    assert "等待期间可以先量好调味料" not in prompt["speech"]
    started = session.handle("开始计时")
    assert "开始计时 10 分钟" in started["speech"]
    assert "在这段时间里你可以同步做自己想做的事情，时间到了我会叫你～" in started["speech"]
    assert session.timer is not None


def test_spoken_progress_intent_tolerates_asr_near_misses_and_keeps_timer_safety() -> None:
    now = [100.0]
    session = make_session(clock=lambda: now[0])
    session.current_recipe = RecipeNormalizer().normalize({
        "name": "测试腌制",
        "ingredients": [{"name": "鸡肉", "amount": 200, "unit": "克"}],
        "steps": [
            {"instruction": "鸡肉加料酒抓匀腌制10分钟"},
            {"instruction": "准备生抽和盐"},
        ],
    })
    session.state = COOKING

    started = session.handle("我弄哈了，计个时呗")
    assert "开始计时 10 分钟" in started["speech"]
    assert session.timer is not None

    blocked = session.handle("我做哈了")
    assert "确认结束计时" in blocked["display"]
    assert session.step_index == 0

    session.handle("继续计时")
    assert session.timer is not None

    now[0] += 600
    timer_finished = session.poll()
    assert timer_finished is not None
    assert "并行准备" in timer_finished["display"]
    session.handle("对")
    advanced = session.handle("这步我搞哈了")
    assert advanced["kitchen_state"] == COMPLETED


def test_non_timed_prep_does_not_inherit_a_timer() -> None:
    recipe = RecipeNormalizer().normalize({
        "name": "番茄鸡蛋面",
        "ingredients": [{"name": "番茄", "amount": 1}],
        "steps": [{"instruction": "番茄洗净切块", "duration_seconds": 120}],
    })
    assert recipe["steps"][0]["duration_seconds"] is None


def test_paused_timer_freezes_and_resumes_from_remaining_time() -> None:
    now = [100.0]
    session = make_session(clock=lambda: now[0])
    start_known_dish(session)
    choose_and_confirm(session)
    session.handle("帮我计时十秒")
    now[0] = 103.0
    session.handle("暂停一下")
    now[0] = 1000.0
    assert session.poll() is None
    assert "7 秒" in session.handle("还有多久")["speech"]
    session.handle("继续")
    now[0] = 1006.0
    assert "1 秒" in session.handle("还有多久")["speech"]
    now[0] = 1008.0
    assert "计时结束" in session.poll()["speech"]


def test_invalid_detail_never_substitutes_an_unrelated_fallback_recipe() -> None:
    fallback = MockRecipeSearchProvider(SKILL_ROOT / "recipes")

    class InvalidDetailProvider:
        mode = "ai_generated"

        def __init__(self) -> None:
            self.fallback = fallback

        def search_recipes(self, request):
            return [RecipeCandidate(
                candidate_id="clear_noodles", title="清汤面", source_name="测试生成", source_url=None,
                summary="测试候选。", estimated_minutes=10, difficulty="简单",
                main_ingredients=["面条"], missing_ingredients=[], match_reason="测试。",
            )]

        def get_recipe_detail(self, candidate):
            return {"name": "无效菜谱", "ingredients": [], "steps": []}

        def fallback_recipe(self):
            return fallback.fallback_recipe()

    session = make_session(provider=InvalidDetailProvider())
    session.handle("我想做清汤面")
    session.handle("一个人，不吃鸡蛋")
    session.handle("正常")
    session.handle("第一个")
    response = session.handle("开始吧")
    assert response["kitchen_state"] == WAITING_RECIPE_CONFIRMATION
    assert session.current_recipe is None
    assert "详情失败" in response["display"]


def test_user_language_catalogs_are_kept_out_of_session_logic() -> None:
    assert len(SINGLE_DINER_COMPANIONS) == 6
    assert len(STEP_ENCOURAGEMENTS) >= 27
    assert len(FINISHED_RESPONSES) == 20
    assert "哇！好香啊！{dish}大功告成！" in FINISHED_RESPONSES
    assert "今天这顿饭必须给自己点个赞！{dish}完成啦！" in FINISHED_RESPONSES


def test_raw_meat_requires_safe_thaw_confirmation_and_steak_timer_flow() -> None:
    now = [100.0]
    fallback = MockRecipeSearchProvider(SKILL_ROOT / "recipes")

    class SteakProvider:
        mode = "mock"

        def __init__(self, fallback_provider) -> None:
            self.fallback = fallback_provider

        def search_recipes(self, request: RecipeSearchRequest):
            assert request.steak_doneness == "五分熟"
            return [RecipeCandidate(
                candidate_id="steak", title="煎牛排", source_name="本地测试", source_url=None,
                summary="适合一人份的煎牛排。", estimated_minutes=12, difficulty="简单",
                main_ingredients=["牛排"], missing_ingredients=[], match_reason="按熟度计时。",
            )]

        def get_recipe_detail(self, candidate: RecipeCandidate):
            return {
                "recipe_id": candidate.candidate_id, "name": "煎牛排", "source_name": "本地测试",
                "servings": 1, "ingredients": [{"name": "牛排", "amount": 1, "unit": "块"}],
                "steps": [
                    {"instruction": "牛排擦干表面并静置回温。"},
                    {"instruction": "锅热后下油，放入牛排正面煎。", "duration_seconds": 10, "heat_level": "中大火"},
                    {"instruction": "翻面后煎另一面。", "duration_seconds": 8, "heat_level": "中大火"},
                    {"instruction": "关火后移到盘中静置。", "duration_seconds": None},
                ],
            }

    session = make_session(clock=lambda: now[0], provider=SteakProvider(fallback))
    session.handle("我要做煎牛排")
    doneness = session.handle("1")
    assert "几成熟" in doneness["question"]
    thickness = session.handle("五分熟")
    assert "多厚" in thickness["question"]
    assert "口味" in session.handle("普通厚度")["question"]
    candidates = session.handle("正常")
    assert candidates["kitchen_state"] == PRESENTING_CANDIDATES
    session.handle("第一个")
    thaw = session.handle("开始吧")
    assert thaw["kitchen_state"] == WAITING_MEAT_THAW
    assert "解冻" in thaw["question"]
    not_thawed = session.handle("还没有解冻")
    assert not_thawed["kitchen_state"] == WAITING_MEAT_THAW
    assert "微波炉" in not_thawed["speech"] and "室温" in not_thawed["speech"]
    cooking = session.handle("解冻好了")
    assert cooking["kitchen_state"] == COOKING
    session.handle("下一步")
    started = session.handle("开始煎")
    assert "正面开始计时 10 秒" in started["speech"]
    now[0] = 111.0
    flip = session.poll()
    assert flip is not None and "翻面好了" in flip["speech"]
    second_side = session.handle("翻面好了")
    assert "另一面开始计时 8 秒" in "".join(item.get("speech", "") for item in items(second_side))
    now[0] = 120.0
    rested = session.poll()
    assert rested is not None and "静置" in "".join(item["speech"] for item in items(rested))
    assert session.step_index == 3


@pytest.mark.parametrize(
    ("question", "required", "safety"),
    [
        ("我拿了一个小锅，面太长放不进去，可以先软化再压进去吗？", "先把面条一端", "NORMAL"),
        ("面条粘在一起了怎么办", "轻轻搅动", "NORMAL"),
        ("我怎么看水有没有烧开？", "持续冒出", "NORMAL"),
        ("水放多了怎么办", "水多一点", "NORMAL"),
        ("水放少了怎么办", "补加开水", "CAUTION"),
        ("我没有白糖怎么办", "可以不放", "NORMAL"),
        ("我没有葱，可以不放吗？", "可以不放葱", "NORMAL"),
        ("我不吃辣", "不放辣椒", "NORMAL"),
        ("锅太小怎么办", "减少一次下锅", "CAUTION"),
        ("没有高压锅怎么办", "普通带盖汤锅", "CAUTION"),
        ("现在用什么火？", "当前这一步", "CAUTION"),
        ("油温过高怎么办", "调小火", "CAUTION"),
        ("油一直在溅", "调小火", "CAUTION"),
        ("我忘记放调料", "少量补", "NORMAL"),
        ("锅里大量冒烟了", "关闭加热", "STOP_AND_CHECK"),
        ("锅里起火了", "不要向油火泼水", "STOP_AND_CHECK"),
        ("我闻到燃气味", "不要开关电器", "STOP_AND_CHECK"),
    ],
)
def test_rule_based_questions_cover_normal_and_safety_cases(question: str, required: str, safety: str) -> None:
    session = make_session()
    start_known_dish(session)
    choose_and_confirm(session)
    before = session.step_index
    response = session.handle(question)
    assert required in (response.get("speech") or "")
    assert response["safety_level"] == safety
    assert session.step_index == before
    if safety == "STOP_AND_CHECK":
        assert session.state == PAUSED
        assert response["robot_action"] == "stop"
        assert response["led_effect"] == "red"
    else:
        assert session.state == COOKING
        assert session.handle("下一步")["kitchen_state"] == COOKING


def test_normalizer_normalizes_steps_maps_feedback_and_rejects_invalid_data() -> None:
    normalizer = RecipeNormalizer()
    recipe = normalizer.normalize({
        "recipe_id": "raw", "name": "测试面", "source_name": "测试来源", "source_url": None,
        "estimated_minutes": 12,
        "ingredients": ["面条", {"name": "鸡蛋", "amount": "1", "unit": "个"}],
        "steps": [{"step_number": 9, "instruction": "切番茄后倒入锅中。"}, {"instruction": "完成后装盘。"}],
    })
    assert [step["step_number"] for step in recipe["steps"]] == [1, 2]
    assert recipe["steps"][0]["robot_action"] == "show_concern"
    assert recipe["steps"][0]["led_effect"] == "blue_dynamic"
    assert recipe["source_name"] == "测试来源"
    assert recipe["estimated_time_minutes"] == 12
    assert recipe["ingredients"][1]["unit"] == "个"
    with pytest.raises(RecipeNormalizationError):
        normalizer.normalize({"ingredients": [{"name": "鸡蛋"}], "steps": [{"instruction": "做"}]})
    with pytest.raises(RecipeNormalizationError):
        normalizer.normalize({"name": "无步骤", "ingredients": [{"name": "鸡蛋"}], "steps": []})


def test_normalizer_only_keeps_heat_timers_and_removes_duplicate_thaw_work() -> None:
    recipe = RecipeNormalizer().normalize({
        "name": "肥牛饭",
        "ingredients": [{"name": "肥牛片", "amount": 200, "unit": "克"}],
        "steps": [
            {"instruction": "淘洗大米", "duration_seconds": 120},
            {"instruction": "洋葱切丝，冷冻肥牛片用温水快速解冻", "duration_seconds": 60, "safety_note": "解冻生肉后洗手"},
            {"instruction": "小火煮肥牛至变色", "duration_seconds": 90, "heat_level": "小火"},
        ],
    })
    assert [step["step_number"] for step in recipe["steps"]] == [1, 2, 3]
    assert recipe["steps"][0]["duration_seconds"] is None
    assert recipe["steps"][1]["instruction"] == "洋葱切丝"
    assert recipe["steps"][1]["duration_seconds"] is None
    assert recipe["steps"][1]["safety_note"] is None
    assert recipe["steps"][2]["duration_seconds"] == 90
    assert all("解冻" not in step["instruction"] for step in recipe["steps"])


def test_normalizer_splits_dense_prep_and_clamps_unrealistic_stir_fry_times() -> None:
    recipe = RecipeNormalizer().normalize({
        "name": "鱼香肉丝",
        "ingredients": [
            {"name": "猪肉丝", "amount": 100, "unit": "克"},
            {"name": "泡发木耳", "amount": 50, "unit": "克"},
            {"name": "胡萝卜", "amount": 50, "unit": "克"},
        ],
        "steps": [
            {"instruction": "将猪肉丝放入小碗，加料酒、玉米淀粉、盐，抓匀腌制，泡发木耳切丝，胡萝卜切丝，泡椒切碎，姜蒜切碎（可选）"},
            {"instruction": "下入猪肉丝快速翻炒", "duration_seconds": 30},
            {"instruction": "放入木耳和胡萝卜翻炒至变软", "duration_seconds": 60},
        ],
    })
    assert "腌制" in recipe["steps"][0]["instruction"] and "木耳" not in recipe["steps"][0]["instruction"]
    assert "泡发木耳" in recipe["steps"][1]["instruction"]
    assert all(word in recipe["steps"][2]["instruction"] for word in ("胡萝卜", "泡椒", "姜蒜"))
    assert recipe["steps"][3]["duration_seconds"] == 120
    assert "完全变色" in recipe["steps"][3]["instruction"]
    assert recipe["steps"][4]["duration_seconds"] == 180
    assert "实际" in recipe["steps"][4]["safety_note"]


def test_thanks_can_route_to_kitchen_without_starting_a_new_session() -> None:
    manager = SkillManager()
    manager.load_skills()
    response = manager.run_user_text("谢谢")
    assert response["selected_skill"] == "kitchen_assistant"
    assert response["session_active"] is False
    assert "不客气" in response["speech"]


def test_provider_is_offline_and_online_placeholder_falls_back_without_fake_source() -> None:
    mock = MockRecipeSearchProvider(SKILL_ROOT / "recipes")
    request = RecipeSearchRequest(requested_dish="番茄鸡蛋面", servings=1, taste_preferences=["少盐"])
    candidates = mock.search_recipes(request)
    assert candidates[0].source_name == "Mock Recipe Provider"
    assert candidates[0].source_url is None
    online = OnlineRecipeSearchProvider(mock)
    assert online.mode == "web_search"
    session = make_session(provider=online)
    start_known_dish(session)
    response = session.handle("第一个")
    assert response["provider_mode"] == "mock"


def test_provider_timeout_and_bad_detail_fall_back_to_local_recipe() -> None:
    mock = MockRecipeSearchProvider(SKILL_ROOT / "recipes")

    class TimeoutProvider:
        mode = "web_search"
        fallback = mock

        def search_recipes(self, request: RecipeSearchRequest):
            raise TimeoutError("simulated offline timeout")

        def get_recipe_detail(self, candidate):
            return {"name": "坏数据", "ingredients": [], "steps": []}

    session = make_session(provider=TimeoutProvider())
    response = start_known_dish(session)
    assert response["provider_mode"] == "mock"
    assert any("离线" in item.get("display", "") for item in items(response))
    session.handle("第一个")
    cooking = session.handle("开始吧")
    assert cooking["kitchen_state"] == COOKING
    assert session.current_recipe["name"] == "番茄鸡蛋面"


def test_executor_executes_multimodal_search_candidate_and_safety_feedback(capsys: pytest.CaptureFixture[str]) -> None:
    session = make_session()
    response = start_known_dish(session)
    executor = RuntimeExecutor(no_play=True)
    executor.execute_plan(response)
    session.handle("第一个")
    safety = session.handle("开始吧")
    executor.execute_plan(safety)
    danger = session.handle("锅里起火了")
    executor.execute_plan(danger)
    output = capsys.readouterr().out
    for marker in ("模拟SDK-灯带", "模拟SDK-表情", "模拟SDK-动作", "模拟SDK-屏幕", "模拟SDK-语音请求"):
        assert marker in output
    assert "effect=red" in output


def test_mock_sdk_unknown_values_still_degrade_safely(capsys: pytest.CaptureFixture[str]) -> None:
    executor = RuntimeExecutor(no_play=True)
    executor.execute_plan({"speech": "测试", "robot_action": "not_real", "led_effect": "not_real", "expression": "not_real"})
    output = capsys.readouterr().out
    assert "未知动作" in output and "未知灯带效果" in output and "未知表情" in output
