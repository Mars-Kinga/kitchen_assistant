from __future__ import annotations

import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = ROOT / "skills" / "kitchen_assistant"
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

from kitchen.models import CookingContext, RecipeCandidate  # noqa: E402
from kitchen.recipe_normalizer import RecipeNormalizer  # noqa: E402
from kitchen.request_parser import parse_updates  # noqa: E402
from kitchen.session_store import KitchenSession  # noqa: E402
from kitchen.states import COOKING  # noqa: E402


def _speech(response: dict) -> str:
    values = response.get("steps", [response])
    return " ".join(str(item.get("speech", item.get("question", ""))) for item in values)


def _cooking_session(*, clock=lambda: 100.0) -> KitchenSession:
    session = KitchenSession(clock=clock)
    session.current_recipe = RecipeNormalizer().normalize(
        {
            "name": "测试牛肉菜",
            "ingredients": [
                {"name": "牛肉", "amount": 200, "unit": "克"},
                {"name": "生抽", "amount": 10, "unit": "毫升"},
                {"name": "盐", "amount": 1, "unit": "克"},
            ],
            "steps": [
                {"instruction": "牛肉下锅，中火翻炒至完全变色、中心无粉红。", "duration_seconds": 120},
                {"instruction": "关火装盘。"},
            ],
        }
    )
    session.state = COOKING
    return session


def test_natural_dish_and_pantry_phrases_are_extracted() -> None:
    assert parse_updates("教我烧土豆牛腩").requested_dish == "土豆牛腩"
    assert parse_updates("我想弄个蘑菇牛肉汤").requested_dish == "蘑菇牛肉汤"

    updates = parse_updates("我手上有土豆、胡萝卜和一点牛腩")
    assert {"土豆", "胡萝卜", "牛腩"} <= set(updates.ingredients)


def test_asr_near_miss_keeps_carrot_as_a_pantry_ingredient() -> None:
    updates = parse_updates("我只有胡萝鸡肉，做什么？厨房助手")

    assert updates.ingredients == ["胡萝卜", "鸡肉"]


def test_asr_near_miss_does_not_recommend_a_recipe_missing_carrot() -> None:
    session = KitchenSession()
    session.handle("我只有胡萝鸡肉，做什么？厨房助手")
    session.handle("一人")
    response = session.handle("正常")

    assert session.request.available_ingredients == ["胡萝卜", "鸡肉"]
    assert all("胡萝卜" in candidate["main_ingredients"] for candidate in response["recipe_candidates"])


class _FreshSearchSpy:
    supports_ai = True
    mode = "ai_generated"

    def __init__(self) -> None:
        self.requests = []

    def search_recipes(self, request):
        self.requests.append(request)
        return [RecipeCandidate(
            candidate_id=f"fresh-{len(self.requests)}",
            title="联网新菜谱",
            source_name="豆包 AI 生成",
            source_url=None,
            summary="跳过本地缓存后的新候选",
            estimated_minutes=12,
            difficulty="简单",
            main_ingredients=["蘑菇", "牛肉"],
            main_seasonings=["盐"],
            missing_ingredients=[],
            match_reason="fresh search",
        )]

    def get_recipe_detail(self, candidate):
        return {
            "name": candidate.title,
            "ingredients": [{"name": "牛肉", "amount": 150, "unit": "克"}],
            "steps": [{"instruction": "牛肉下锅，煮至完全变色。"}],
        }


def test_explicit_fresh_search_replaces_current_cached_candidates() -> None:
    provider = _FreshSearchSpy()
    session = KitchenSession(recipe_provider=provider)
    session.handle("我只有蘑菇和牛肉")
    session.handle("一人")
    first = session.handle("正常")
    assert first["recipe_candidates"]
    assert len(provider.requests) == 1

    refreshed = session.handle("不要本地的，上网搜索")

    assert len(provider.requests) == 2
    assert provider.requests[-1].bypass_cache is True
    assert refreshed["recipe_candidates"][0]["title"] == "联网新菜谱"
    assert refreshed["provider_mode"] == "ai_generated"


def test_fresh_search_without_ai_does_not_fallback_to_local_candidates() -> None:
    provider = _FreshSearchSpy()
    provider.supports_ai = False
    provider.mode = "local_cache"
    session = KitchenSession(recipe_provider=provider)
    session.handle("我只有蘑菇和牛肉")
    session.handle("一人")
    session.handle("正常")

    response = session.handle("不要本地的，上网搜索")

    assert len(provider.requests) == 1
    assert "不会假装已经完成上网搜索" in _speech(response)
    assert response["recipe_candidates"][0]["title"] == "联网新菜谱"


def test_natural_first_turns_route_to_kitchen_skill() -> None:
    from runtime_core.skill_manager import SkillManager

    manager = SkillManager()
    manager.load_skills()
    assert manager.run_user_text("教我烧土豆牛腩")["selected_skill"] == "kitchen_assistant"

    manager = SkillManager()
    manager.load_skills()
    assert manager.run_user_text("我手上有土豆、胡萝卜和一点牛腩")["selected_skill"] == "kitchen_assistant"


def test_dietary_phrases_do_not_turn_no_spicy_into_spicy() -> None:
    updates = parse_updates("我只有鸡蛋和番茄，低脂少盐高蛋白不辣，不吃花生")

    assert updates.taste == "少盐"
    assert updates.ingredients == ["鸡蛋", "番茄"]
    assert "辣" in updates.restrictions
    assert {"低脂", "高蛋白", "花生"} <= set(updates.restrictions)


def test_offline_recommendation_does_not_claim_unverified_dietary_properties() -> None:
    from kitchen.models import RecipeSearchRequest
    from providers.mock_recipe_provider import MockRecipeSearchProvider

    provider = MockRecipeSearchProvider(SKILL_ROOT / "recipes")
    results = provider.search_recipes(
        RecipeSearchRequest(
            requested_dish="番茄炒蛋",
            servings=2,
            taste_preferences=["少盐"],
            dietary_restrictions=["低脂"],
        )
    )
    assert results == []


def test_dietary_restrictions_survive_a_complete_pantry_conversation() -> None:
    session = KitchenSession()
    session.handle("我只有鸡蛋和番茄，低脂少盐高蛋白不辣，不吃花生")
    response = session.handle("两个人")

    assert session.request.available_ingredients == ["鸡蛋", "番茄"]
    assert {"低脂", "高蛋白", "花生", "辣"} <= set(session.request.dietary_restrictions)
    assert response["recipe_candidates"] == []
    assert "不会忽略" in response["steps"][-1]["speech"] or "没有找到" in response["steps"][-1]["speech"]


def test_cooking_questions_answer_locally_without_advancing() -> None:
    session = _cooking_session()
    questions = {
        "没有生抽怎么办": "替代",
        "可以用鸡肉代替牛肉吗": "鸡肉",
        "水还没有开": "烧开",
        "肉还是粉红色": "继续",
        "好像太咸了": "淡",
        "锅里快干了": "开水",
        "这一步什么意思": "牛肉",
        "现在做到哪一步了": "第 1",
    }

    for question, expected in questions.items():
        before = session.step_index
        response = session.handle(question)
        assert session.step_index == before, question
        assert expected in _speech(response), question


def test_continue_timing_does_not_request_early_skip_confirmation() -> None:
    session = _cooking_session()
    session.handle("开始计时")
    response = session.handle("继续计时")

    assert session.timer is not None
    assert "确认结束计时" not in _speech(response)
    assert "继续" in _speech(response)


def test_timer_parser_accepts_spoken_time_marker() -> None:
    session = _cooking_session()
    response = session.handle("给我记五分钟")

    assert session.timer is not None
    assert "5 分钟" in _speech(response)


def test_equipment_limits_are_parsed_as_available_choices() -> None:
    updates = parse_updates("我没有高压锅、烤箱、空气炸锅和炒锅，只有电饭煲")

    assert updates.equipment == ["电饭煲"]
    assert getattr(updates, "unavailable_equipment", []) == ["高压锅", "烤箱", "空气炸锅", "炒锅"]


def test_ai_recipe_validation_keeps_equipment_constraints() -> None:
    from kitchen.models import RecipeSearchRequest
    from providers.doubao_ai_recipe_provider import _ensure_equipment_constraints

    request = RecipeSearchRequest(unavailable_equipment=["炒锅"], equipment_only=True, available_equipment=["电饭煲"])
    with pytest.raises(ValueError, match="厨具"):
        _ensure_equipment_constraints({"equipment": ["炒锅"]}, request)
    with pytest.raises(ValueError, match="厨具"):
        _ensure_equipment_constraints({"equipment": ["烤箱"]}, request)


def test_rice_cooker_only_flow_selects_a_compatible_cached_recipe() -> None:
    from providers.mock_recipe_provider import MockRecipeSearchProvider

    provider = MockRecipeSearchProvider(SKILL_ROOT / "recipes", generated_dir=SKILL_ROOT / "recipes" / "generated")
    session = KitchenSession(recipe_provider=provider)
    session.handle("我想做糖醋排骨，没有炒锅，只有电饭煲")
    session.handle("三个人")
    response = session.handle("正常")

    assert response["recipe_candidates"]
    assert response["recipe_candidates"][0]["title"] == "电饭煲版糖醋排骨"
    assert session.request.unavailable_equipment == ["炒锅"]


def test_fixed_catalog_does_not_ask_user_to_wash_raw_ribs() -> None:
    from kitchen.dish_profiles import load_catalog

    recipes, _ = load_catalog(SKILL_ROOT / "recipes")
    normalizer = RecipeNormalizer()
    normalized = [normalizer.normalize(recipe) for recipe in recipes]

    raw_meat_washing = [
        step["instruction"]
        for recipe in normalized
        for step in recipe["steps"]
        if any(meat in step["instruction"] for meat in ("排骨", "牛肉", "鸡肉"))
        and "洗净" in step["instruction"]
    ]
    assert raw_meat_washing == []


def test_adjacent_marinade_instructions_share_one_waiting_timer() -> None:
    recipe = RecipeNormalizer().normalize(
        {
            "name": "测试重复腌制",
            "ingredients": [
                {"name": "牛肉", "amount": 150, "unit": "克"},
                {"name": "料酒", "amount": 10, "unit": "毫升"},
                {"name": "盐", "amount": 1, "unit": "克"},
            ],
            "steps": [
                {"instruction": "把牛肉放入腌制碗中，腌制10分钟"},
                {"instruction": "加入10毫升料酒、1克盐，抓匀后腌制，腌制15分钟"},
                {"instruction": "热锅后放入牛肉翻炒至完全变色"},
            ],
        }
    )

    marinade_steps = [step for step in recipe["steps"] if "腌制" in step["instruction"] and "腌制好的" not in step["instruction"]]
    assert len(marinade_steps) == 1
    assert marinade_steps[0]["duration_seconds"] == 900
    assert "10毫升料酒" in marinade_steps[0]["instruction"]


def test_vague_step_amounts_are_not_accepted_for_beginner_recipes() -> None:
    with pytest.raises(ValueError, match="模糊用量"):
        RecipeNormalizer().normalize(
            {
                "name": "测试模糊用量",
                "ingredients": [{"name": "牛肉", "amount": 150, "unit": "克"}],
                "steps": [{"instruction": "锅中加入少量油，放入牛肉翻炒至完全变色"}],
            }
        )


def test_sauce_resting_and_meat_marinating_are_not_two_duplicate_waits() -> None:
    recipe = RecipeNormalizer().normalize(
        {
            "name": "测试糖醋排骨",
            "ingredients": [{"name": "排骨", "amount": 300, "unit": "克"}],
            "steps": [
                {"instruction": "取碗加入调料，搅拌均匀制成腌制调味汁，腌制10分钟"},
                {"instruction": "将排骨放入腌制调味汁中，抓匀后腌制10分钟"},
                {"instruction": "锅中加入排骨焖煮至熟透"},
            ],
        }
    )

    waiting = [step for step in recipe["steps"] if "腌制" in step["instruction"] and "腌制好的" not in step["instruction"]]
    assert len(waiting) == 1
    assert waiting[0]["duration_seconds"] == 600
