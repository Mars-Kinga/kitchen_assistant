from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import sys

import pytest


SKILL_ROOT = Path(__file__).resolve().parents[1] / "skills" / "kitchen_assistant"
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

from kitchen.models import RecipeSearchRequest  # noqa: E402
from kitchen.pantry_defaults import COMMON_SEASONINGS, is_common_seasoning, uses_unavailable_ingredients  # noqa: E402
from kitchen.recommendation_service import rank_recipes  # noqa: E402
from kitchen.request_parser import apply_updates, parse_updates  # noqa: E402
from kitchen.session_store import KitchenSession  # noqa: E402
from llm.prompts import candidate_messages, recipe_bundle_messages, recipe_correction_messages, recipe_messages  # noqa: E402
from providers.qwen_ai_recipe_provider import _prepare_bundle_payload, _request_payload  # noqa: E402


@pytest.mark.parametrize("name", ["植物油", "食用盐", "白砂糖", "生抽", "老抽", "米醋", "料酒", "蚝油", "白胡椒粉", "黑胡椒粉", "玉米淀粉", "芝麻油", "小葱", "姜片", "蒜末"])
def test_common_seasonings_do_not_require_photographic_inventory(name):
    assert is_common_seasoning(name)


@pytest.mark.parametrize("name", ["蜂蜜", "黄油", "奶油", "啤酒", "红酒", "咖喱块", "鸡肉", "牛奶"])
def test_special_ingredients_are_not_assumed_available(name):
    assert not is_common_seasoning(name)


def test_all_pantry_generation_prompts_share_the_policy_without_inflating_inventory():
    request = {"available_ingredients": ["番茄", "鸡蛋"], "servings": 1}
    prompts = [
        candidate_messages(request), recipe_bundle_messages(request),
        recipe_correction_messages(request, {}, {"code": "invalid_recipe", "path": "$", "message": "invalid"}),
        recipe_messages({"title": "番茄炒蛋"}, request),
    ]
    for messages in prompts:
        system = messages[0]["content"]
        assert "不是完整厨房库存" in system
        assert "不计入全食材覆盖或排序" in system
        assert "写清准确用量" in system
        assert all(seasoning in system for seasoning in COMMON_SEASONINGS)
    assert request["available_ingredients"] == ["番茄", "鸡蛋"]


def local_choices():
    return [{
        "recipe_id": f"tomato_{index}", "name": f"番茄做法{index}",
        "ingredients": [{"name": name} for name in ["番茄", "生抽", "米醋", "白砂糖", "食用油", "食用盐", "蒜"]],
        "steps": [{"instruction": "将番茄与蒜切好，加入食用油、生抽、米醋、白砂糖和食用盐翻炒。"}],
        "estimated_time_minutes": 10, "difficulty": "简单",
    } for index in range(2)]


def test_local_matching_does_not_report_unphotographed_common_seasonings_missing():
    request = RecipeSearchRequest(available_ingredients=["番茄"])
    candidates = rank_recipes(local_choices(), request)
    assert len(candidates) == 2
    assert all(candidate.missing_ingredients == [] for candidate in candidates)
    assert all(candidate.unused_ingredients == [] for candidate in candidates)
    assert request.available_ingredients == ["番茄"]


def test_local_matching_filters_explicitly_missing_seasonings():
    request = RecipeSearchRequest(available_ingredients=["番茄"], unavailable_ingredients=["生抽"])
    assert rank_recipes(local_choices(), request) == []


@pytest.mark.parametrize("text,missing", [("没有料酒", ["料酒"]), ("我有番茄鸡蛋，没有盐和生抽", ["盐", "生抽"]), ("家里没油", ["油"]), ("没有调料", ["调料"])])
def test_explicit_absence_is_preserved_separately_from_dietary_restrictions(text, missing):
    request = apply_updates(RecipeSearchRequest(), parse_updates(text))
    assert set(request.unavailable_ingredients) == set(missing)
    assert not request.dietary_restrictions
    assert not set(missing) & set(request.available_ingredients)


def test_absence_parser_does_not_treat_other_negative_sentences_as_missing_food():
    assert parse_updates("我没吃过鸡肉，想试试").unavailable_ingredients == []
    request = apply_updates(RecipeSearchRequest(), parse_updates("没有盐"))
    apply_updates(request, parse_updates("我现在有盐"))
    assert request.unavailable_ingredients == []


def test_absence_does_not_use_broad_allergy_groups_as_substitution_rules():
    assert not uses_unavailable_ingredients(["蜂蜜"], ["白糖"])
    assert uses_unavailable_ingredients(["食用盐"], ["盐"])
    assert uses_unavailable_ingredients(["生抽", "老抽"], ["酱油"])


def generated_bundle():
    rows = []
    for title in ("清炒番茄", "蒜香番茄"):
        rows.append({
            "title": title, "summary": "使用番茄和常备调料。", "estimated_minutes": 10,
            "difficulty": "简单", "main_ingredients": ["番茄"], "main_seasonings": ["生抽"],
            "missing_ingredients": ["生抽"], "match_reason": "按已有番茄制作。",
            "recipe": {"title": title, "servings": 1, "estimated_minutes": 10, "difficulty": "简单",
                "ingredients": [{"name": "番茄", "amount": 200, "unit": "克", "optional": False},
                                {"name": "生抽", "amount": 5, "unit": "毫升", "optional": False}],
                "equipment": ["炒锅"], "steps": [{"instruction": "番茄200克洗净切块，放入锅中，加入生抽5毫升翻炒。"}],
            },
        })
    return {"candidates": rows}


def test_generated_recipe_can_use_common_seasonings_not_in_the_photo():
    request = RecipeSearchRequest(available_ingredients=["番茄"], servings=1)
    prepared = _prepare_bundle_payload(generated_bundle(), request, 3)
    assert len(prepared) == 2
    assert prepared[0][0].unused_ingredients == []
    assert prepared[0][0].missing_ingredients == []
    assert any(item["name"] == "生抽" and item["amount"] == 5 for item in prepared[0][1]["ingredients"])


def test_generated_recipe_cannot_override_explicit_absence_with_defaults():
    request = RecipeSearchRequest(available_ingredients=["番茄"], servings=1, unavailable_ingredients=["生抽"])
    assert _request_payload(request)["unavailable_ingredients"] == ["生抽"]
    assert json.loads(recipe_bundle_messages(_request_payload(request))[1]["content"])["unavailable_ingredients"] == ["生抽"]
    with pytest.raises(Exception, match="用户明确没有"):
        _prepare_bundle_payload(generated_bundle(), request, 3)
    assert request.as_cache_key() != RecipeSearchRequest(available_ingredients=["番茄"], servings=1).as_cache_key()


def test_voice_absence_after_recommendation_reselects_compatible_local_recipes():
    class LocalProvider:
        mode = "mock"

        def search_recipes(self, request):
            return rank_recipes(deepcopy(local_choices()), request)

    session = KitchenSession(recipe_provider=LocalProvider())
    result = session.recommend_from_ingredients(["番茄"], servings=1)
    assert result["recipe_candidates"]
    result = session.handle("没有生抽")
    assert session.request.unavailable_ingredients == ["生抽"]
    assert result["recipe_candidates"] == []
