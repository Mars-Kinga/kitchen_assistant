from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import sys

import pytest


SKILL_ROOT = Path(__file__).resolve().parents[1] / "skills" / "kitchen_assistant"
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

from kitchen.ingredient_vocabulary import ingredient_present, inventory_ingredient_present  # noqa: E402
from kitchen.models import RecipeSearchRequest  # noqa: E402
from kitchen.recipe_normalizer import RecipeNormalizer  # noqa: E402
from kitchen.recipe_contract import MAX_TEXT_LENGTH, bundle_prompt_rules  # noqa: E402
from llm.prompts import candidate_messages, recipe_messages, recipe_bundle_messages, recipe_correction_messages  # noqa: E402
from llm.qwen_client import QwenJSONOutputError  # noqa: E402
from providers.mock_recipe_provider import MockRecipeSearchProvider  # noqa: E402
from providers.qwen_ai_recipe_provider import (  # noqa: E402
    AIRecipeProviderError, QwenAIRecipeProvider, _complete_rows_from_partial_json, _prepare_bundle_payload, _validation_issue,
)


PHOTO_INVENTORY = ["牛肋排", "玉米", "小番茄", "西兰花", "生菜", "青椒", "牛奶", "蜂蜜"]


def photo_bundle():
    quantities = [
        ("牛肋排", 250, "克"), ("玉米", 1, "根"), ("圣女果", 6, "个"),
        ("西蓝花", 120, "克"), ("生菜", 60, "克"), ("青椒", 1, "个"),
        ("牛奶", 200, "毫升"), ("蜂蜜", 10, "克"),
    ]
    instructions = [
        "牛肋排250克用厨房纸擦干，将肉与蔬菜分开处理。",
        "玉米1根去外皮和须，切成3厘米宽的小段。",
        "圣女果6个洗净，对半切开。",
        "西蓝花120克分成小朵，冲净后沥水。",
        "生菜60克逐片洗净，沥水后撕成小片。",
        "青椒1个洗净，去蒂去籽，切成2厘米宽的片。",
        "将玉米放入独立汤锅，加清水没过食材，中火煮10分钟，检查是否达到所需软硬程度。",
        "西蓝花放入煮开的清水中焯2分钟，捞出沥水。",
        "平底锅中加入5毫升食用油，中火放入牛肋排，分面煎制，按肉块厚度检查实际状态。",
        "加入青椒与5克蜂蜜，转小火翻动1分钟，让蜂蜜裹在肉块表面，关火。",
        "把牛肋排、青椒、玉米、西蓝花、生菜和圣女果分区摆入盘中。",
        "另取杯子，倒入牛奶200毫升与剩余5克蜂蜜，搅拌均匀，作为搭配饮品。",
    ]
    first = {
        "title": "蜂蜜牛肋排蔬菜拼盘配牛奶", "summary": "主菜、配菜与饮品分开制作。",
        "estimated_minutes": 35, "difficulty": "中等", "main_ingredients": [item[0] for item in quantities],
        "main_seasonings": [], "missing_ingredients": [], "match_reason": "使用全部拍摄食材。",
        "recipe": {
            "title": "蜂蜜牛肋排蔬菜拼盘配牛奶", "servings": 1, "estimated_minutes": 35,
            "difficulty": "中等", "equipment": ["平底锅", "汤锅", "杯子"],
            "ingredients": [{"name": name, "amount": amount, "unit": unit, "optional": False}
                            for name, amount, unit in quantities]
                           + [{"name": "食用油", "amount": 5, "unit": "毫升", "optional": False}],
            "steps": [{"instruction": instruction} for instruction in instructions],
        },
    }
    second = {
        "title": "蜂蜜牛奶", "summary": "只使用牛奶和蜂蜜。", "estimated_minutes": 5,
        "difficulty": "简单", "main_ingredients": ["牛奶"], "main_seasonings": ["蜂蜜"],
        "missing_ingredients": [], "match_reason": "可以单独准备饮品。",
        "recipe": {
            "title": "蜂蜜牛奶", "servings": 1, "estimated_minutes": 5, "difficulty": "简单",
            "equipment": ["杯子"], "ingredients": [
                {"name": "牛奶", "amount": 200, "unit": "毫升", "optional": False},
                {"name": "蜂蜜", "amount": 10, "unit": "克", "optional": False},
            ], "steps": [{"instruction": "倒入牛奶200毫升，再加入蜂蜜10克，搅拌均匀。"}],
        },
    }
    return {"candidates": [first, second]}


class FixtureLLM:
    timeout = 25

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def is_available(self):
        return True

    def generate_json(self, messages, **kwargs):
        self.calls.append({"messages": messages, **kwargs})
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return deepcopy(response)


class CloudOnlyProvider(MockRecipeSearchProvider):
    def search_recipes(self, request):
        return []


@pytest.mark.parametrize("inventory,instruction", [
    ("西兰花", "把西蓝花切成小朵，放入锅中。"),
    ("小番茄", "圣女果洗净，对半切开。"),
    ("小番茄", "将樱桃番茄拌入生菜。"),
    ("玉米", "将甜玉米煮10分钟。"),
    ("葱", "加入小葱，翻拌均匀。"),
    ("牛肉", "将牛肋排分面煎制。"),
])
def test_inventory_aliases_are_recognized_inside_instructions(inventory, instruction):
    assert inventory_ingredient_present(inventory, [instruction])
    assert ingredient_present(inventory, [instruction])


@pytest.mark.parametrize("inventory,substitute", [
    ("牛奶", "黄油"), ("蜂蜜", "白糖"), ("青椒", "红椒"),
    ("生菜", "菠菜"), ("牛肋排", "牛排"), ("葱", "洋葱"),
    ("玉米", "玉米淀粉"), ("玉米", "玉米油"), ("番茄", "番茄酱"),
])
def test_same_category_or_substring_is_not_actual_inventory_use(inventory, substitute):
    assert not inventory_ingredient_present(inventory, [substitute])
    assert not inventory_ingredient_present(inventory, [f"加入{substitute}，翻拌均匀。"])


def test_photo_inventory_bundle_passes_without_correction_and_is_reused_locally(tmp_path):
    request = RecipeSearchRequest(available_ingredients=PHOTO_INVENTORY, servings=1)
    llm = FixtureLLM([photo_bundle()])
    fallback = CloudOnlyProvider(SKILL_ROOT / "recipes", generated_dir=tmp_path / "generated")
    provider = QwenAIRecipeProvider(llm, fallback)
    candidates = provider.search_recipes(request)

    assert len(candidates) == 2
    assert candidates[0].unused_ingredients == []
    assert candidates[1].unused_ingredients == PHOTO_INVENTORY[:6]
    assert len(provider.get_recipe_detail(candidates[0])["steps"]) == 12
    assert len(llm.calls) == 1

    offline_llm = FixtureLLM([])
    cached = QwenAIRecipeProvider(offline_llm, MockRecipeSearchProvider(
        SKILL_ROOT / "recipes", generated_dir=tmp_path / "generated",
    ))
    reused = cached.search_recipes(request)
    assert 2 <= len(reused) <= 3
    assert reused[0].unused_ingredients == []
    assert reused[0].source_name == "已保存菜谱"
    assert offline_llm.calls == []


def test_invalid_first_row_does_not_discard_valid_original_choice_or_retry(tmp_path):
    original = photo_bundle()
    original["candidates"][0]["recipe"]["steps"][3]["instruction"] = "切" * (MAX_TEXT_LENGTH + 1)
    correction = {"candidates": [photo_bundle()["candidates"][0]]}
    llm = FixtureLLM([original, correction])
    provider = QwenAIRecipeProvider(llm, CloudOnlyProvider(
        SKILL_ROOT / "recipes", generated_dir=tmp_path / "generated",
    ))

    candidates = provider.search_recipes(RecipeSearchRequest(available_ingredients=PHOTO_INVENTORY, servings=1))
    assert len(candidates) == 1
    assert candidates[0].title == "蜂蜜牛奶"
    assert candidates[0].unused_ingredients == PHOTO_INVENTORY[:6]
    assert len(llm.calls) == 1


def test_invalid_second_row_does_not_discard_valid_first_row():
    bundle = photo_bundle()
    bundle["candidates"][1]["recipe"]["ingredients"][0]["amount"] = "适量"
    prepared = _prepare_bundle_payload(bundle, RecipeSearchRequest(available_ingredients=PHOTO_INVENTORY, servings=1), 3)
    assert len(prepared) == 1
    assert prepared[0][0].unused_ingredients == []


def test_pantry_prompt_allows_separate_components_and_detailed_steps():
    rules = "\n".join(bundle_prompt_rules({"available_ingredients": PHOTO_INVENTORY}))
    assert "优先生成2个" in rules
    assert "12至18步" in rules
    assert "保留用户食材名称" in rules


MEAL_INVENTORY = ["番茄", "鸡蛋", "黄瓜", "西兰花"]


def meal_bundle(dish_count):
    first = {
        "title": "番茄炒蛋＋清炒黄瓜西兰花" if dish_count == 2 else "番茄炒蛋＋清炒黄瓜＋清炒西兰花",
        "summary": "各道菜分别制作，整套用上全部食材。", "estimated_minutes": 35,
        "difficulty": "简单", "main_ingredients": MEAL_INVENTORY,
        "main_seasonings": ["食用油", "盐"], "missing_ingredients": [], "match_reason": "整套使用全部拍摄食材。",
    }
    quantities = [("番茄", 100, "克"), ("鸡蛋", 1, "个"), ("黄瓜", 150, "克"), ("西兰花", 150, "克")]
    instructions = [
        ("【第1道】番茄100克洗净，去蒂后切成2厘米块。", None),
        ("【第1道】鸡蛋1个打入碗中，用筷子打至蛋黄蛋白混合均匀。", None),
        ("【第1道】炒锅加入食用油5毫升，中火加热30秒。", 30),
        ("【第1道】倒入蛋液，中火翻动1分钟，待蛋液凝固后盛到干净盘中。", 60),
        ("【第1道】锅中再加食用油5毫升，放入番茄，中火翻炒2分钟至出汁。", 120),
        ("【第1道】放回鸡蛋，加入盐1克，中火翻炒1分钟，让番茄汁裹匀蛋块。", 60),
        ("【第1道】关火，将番茄和鸡蛋一起盛出装盘。", None),
        ("【第2道】黄瓜150克洗净，去掉两端，切成约3毫米厚的片。", None),
    ]
    if dish_count == 2:
        instructions += [
            ("【第2道】西兰花150克分成小朵，冲净并沥水。", None),
            ("【第2道】洗净炒锅，倒入食用油10毫升，中火加热30秒。", 30),
            ("【第2道】加入西兰花和清水30毫升，中火翻炒3分钟，检查茎部达到所需软硬程度。", 180),
            ("【第2道】加入黄瓜与盐2克，中火翻炒2分钟至黄瓜表面变软。", 120),
            ("【第2道】关火，将黄瓜和西兰花盛入另一只盘子。", None),
        ]
    else:
        instructions += [
            ("【第2道】洗净炒锅，倒入食用油5毫升，中火加热30秒。", 30),
            ("【第2道】加入黄瓜，中火翻炒2分钟，检查黄瓜边缘开始变软。", 120),
            ("【第2道】加入盐1克，翻拌均匀。", None),
            ("【第2道】关火，将黄瓜盛入另一只盘子。", None),
            ("【第3道】西兰花150克分成小朵，冲净并沥水。", None),
            ("【第3道】洗净炒锅，倒入食用油5毫升，中火加热30秒。", 30),
            ("【第3道】加入西兰花和清水30毫升，中火翻炒3分钟，检查茎部达到所需软硬程度。", 180),
            ("【第3道】加入盐1克，翻拌均匀。", None),
            ("【第3道】关火，将西兰花单独盛入盘子。", None),
        ]
    first["recipe"] = {
        "title": first["title"], "servings": 1, "estimated_minutes": 35, "difficulty": "简单",
        "equipment": ["炒锅"],
        "ingredients": [{"name": name, "amount": amount, "unit": unit, "optional": False}
                        for name, amount, unit in quantities + [("食用油", 20, "毫升"), ("盐", 3, "克"), ("清水", 30, "毫升")]],
        "steps": [{"instruction": text, "duration_seconds": duration} for text, duration in instructions],
    }
    second = deepcopy(first)
    second["title"] = second["recipe"]["title"] = "清炒黄瓜"
    second["main_ingredients"] = ["黄瓜"]
    second["recipe"]["ingredients"] = [
        {"name": "黄瓜", "amount": 150, "unit": "克", "optional": False},
        {"name": "食用油", "amount": 5, "unit": "毫升", "optional": False},
        {"name": "盐", "amount": 1, "unit": "克", "optional": False},
    ]
    second["recipe"]["steps"] = [
        {"instruction": "黄瓜150克洗净切片。"},
        {"instruction": "炒锅加入食用油5毫升，放入黄瓜和盐1克，中火翻炒2分钟，检查黄瓜达到所需软硬程度。", "duration_seconds": 120},
        {"instruction": "关火，将黄瓜盛出装盘。"},
    ]
    return {"candidates": [first, second]}


def test_all_generation_stages_distinguish_alternative_plans_from_dish_count():
    request = {"available_ingredients": MEAL_INVENTORY, "servings": 1}
    messages = [candidate_messages(request), recipe_messages({"title": "饭菜组合"}, request),
                recipe_bundle_messages(request), recipe_correction_messages(request, {}, {"message": "invalid"})]
    for prompt in messages:
        assert "候选是备选方案数" in prompt[0]["content"]
        assert "整套饭菜合起来使用全部确认食材" in prompt[0]["content"]
        assert "只有1份可行时也返回" in prompt[0]["content"]
        assert "优先组合两道或三道菜" in prompt[0]["content"]


@pytest.mark.parametrize("dish_count", [2, 3])
def test_multi_dish_meal_validates_union_preserves_servings_and_reuses_cache(tmp_path, dish_count):
    request = RecipeSearchRequest(available_ingredients=MEAL_INVENTORY, servings=1)
    llm = FixtureLLM([meal_bundle(dish_count)])
    provider = QwenAIRecipeProvider(llm, CloudOnlyProvider(SKILL_ROOT / "recipes", generated_dir=tmp_path))
    candidates = provider.search_recipes(request)
    assert len(candidates) == 2
    assert candidates[0].title.count("＋") == dish_count - 1
    assert candidates[0].unused_ingredients == []
    assert candidates[1].unused_ingredients == ["番茄", "鸡蛋", "西兰花"]
    detail = provider.get_recipe_detail(candidates[0])
    normalized = RecipeNormalizer().normalize(detail, servings=3)
    assert normalized["servings"] == 3
    assert {f"【第{index}道】" for index in range(1, dish_count + 1)} == {
        step["instruction"].split("】", 1)[0] + "】" for step in normalized["steps"]
    }
    prep = next(step for step in normalized["steps"] if "黄瓜450克洗净" in step["instruction"])
    assert prep["duration_seconds"] is None
    assert any(step["duration_seconds"] for step in normalized["steps"])
    assert len(llm.calls) == 1
    offline = FixtureLLM([])
    cached = QwenAIRecipeProvider(offline, MockRecipeSearchProvider(SKILL_ROOT / "recipes", generated_dir=tmp_path))
    reused = cached.search_recipes(request)
    assert reused[0].unused_ingredients == []
    assert reused[0].title == candidates[0].title
    assert offline.calls == []


def test_multi_dish_meal_still_requires_real_use_not_only_total_ingredient_list():
    bundle = meal_bundle(3)
    first = bundle["candidates"][0]
    first["recipe"]["steps"] = [step for step in first["recipe"]["steps"] if not step["instruction"].startswith("【第3道】")]
    prepared = _prepare_bundle_payload(bundle, RecipeSearchRequest(available_ingredients=MEAL_INVENTORY, servings=1), 3)
    assert prepared[0][0].unused_ingredients == ["西兰花"]


@pytest.mark.parametrize("fenced", [False, True])
def test_truncated_later_json_row_preserves_complete_partial_recipe_without_retry(tmp_path, fenced):
    valid = meal_bundle(2)["candidates"][1]
    raw = '{"candidates":[' + json.dumps(valid, ensure_ascii=False) + ',{"title":"unfinished","recipe":{"steps":['
    if fenced:
        raw = "```json\n" + raw
    assert len(_complete_rows_from_partial_json(raw, 3)["candidates"]) == 1
    llm = FixtureLLM([QwenJSONOutputError(raw)])
    provider = QwenAIRecipeProvider(llm, CloudOnlyProvider(SKILL_ROOT / "recipes", generated_dir=tmp_path))
    candidates = provider.search_recipes(RecipeSearchRequest(available_ingredients=MEAL_INVENTORY, servings=1))
    assert len(candidates) == 1
    assert candidates[0].title == "清炒黄瓜"
    assert candidates[0].unused_ingredients == ["番茄", "鸡蛋", "西兰花"]
    assert len(provider.get_recipe_detail(candidates[0])["steps"]) == 3
    assert len(llm.calls) == 1
    assert provider.last_cache_candidate_count == 1


def test_unfinished_recipe_is_not_recovered_by_adding_json_closers(tmp_path):
    raw = '{"candidates":[{"title":"unfinished","recipe":{"ingredients":['
    assert _complete_rows_from_partial_json(raw, 3) == {"candidates": []}
    corrected = {"candidates": [meal_bundle(2)["candidates"][1]]}
    llm = FixtureLLM([QwenJSONOutputError(raw), corrected])
    provider = QwenAIRecipeProvider(llm, CloudOnlyProvider(SKILL_ROOT / "recipes", generated_dir=tmp_path))
    candidates = provider.search_recipes(RecipeSearchRequest(available_ingredients=MEAL_INVENTORY, servings=1))
    assert candidates[0].title == "清炒黄瓜"
    assert len(llm.calls) == 2


@pytest.mark.parametrize("defect", ["no_actual_use", "unavailable", "wrong_servings", "missing_steps", "extra_main_food"])
def test_single_candidate_fallback_never_relaxes_recipe_integrity_or_user_constraints(defect):
    row = deepcopy(meal_bundle(2)["candidates"][1])
    request = RecipeSearchRequest(available_ingredients=MEAL_INVENTORY, servings=1)
    if defect == "no_actual_use":
        row["recipe"]["steps"] = [{"instruction": "食用油5毫升和盐1克搅拌均匀。"}]
    elif defect == "unavailable":
        request.unavailable_ingredients = ["盐"]
    elif defect == "wrong_servings":
        row["recipe"]["servings"] = 2
    elif defect == "missing_steps":
        row["recipe"]["steps"] = []
    else:
        row["recipe"]["ingredients"].append({"name": "鸡肉", "amount": 100, "unit": "克", "optional": False})
    with pytest.raises(AIRecipeProviderError):
        _prepare_bundle_payload({"candidates": [row]}, request, 3)


def test_only_one_partial_local_recipe_is_returned_without_cloud_call():
    from kitchen.recommendation_service import rank_recipes

    row = meal_bundle(2)["candidates"][1]
    recipe = deepcopy(row["recipe"])
    recipe["recipe_id"] = "local_cucumber"
    recipe["name"] = recipe["title"]

    class OneLocalProvider:
        def search_recipes(self, request):
            return rank_recipes([recipe], request)

        def get_recipe_detail(self, candidate):
            return deepcopy(recipe)

    llm = FixtureLLM([])
    provider = QwenAIRecipeProvider(llm, OneLocalProvider())
    candidates = provider.search_recipes(RecipeSearchRequest(available_ingredients=MEAL_INVENTORY, servings=1))
    assert len(candidates) == 1
    assert candidates[0].unused_ingredients == ["番茄", "鸡蛋", "西兰花"]
    assert provider.get_recipe_detail(candidates[0])["steps"]
    assert llm.calls == []
