from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = ROOT / "skills" / "kitchen_assistant"
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

from kitchen.cooking_question_service import DoubaoCookingQuestionService  # noqa: E402
from kitchen.models import CookingContext, RecipeCandidate, RecipeSearchRequest  # noqa: E402
from kitchen.session_store import KitchenSession  # noqa: E402
from kitchen.states import COOKING, PRESENTING_CANDIDATES, WAITING_RECIPE_CONFIRMATION  # noqa: E402
from llm.doubao_client import DoubaoClientError, DoubaoLLMClient, parse_json_response  # noqa: E402
from llm.config import DEFAULT_MAX_RETRIES, DEFAULT_TIMEOUT_SECONDS, DoubaoConfig  # noqa: E402
from llm.prompts import candidate_messages, recipe_bundle_messages, recipe_messages  # noqa: E402
from providers.doubao_ai_recipe_provider import AIRecipeProviderError, DoubaoAIRecipeProvider  # noqa: E402
from providers.mock_recipe_provider import MockRecipeSearchProvider  # noqa: E402
from runtime_core.executor import RuntimeExecutor  # noqa: E402


class FakeCompletions:
    def __init__(self, responses: list[object]) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        if response == "empty_choices":
            return SimpleNamespace(choices=[])
        if response == "empty_content":
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=""))])
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=response))])


class FakeClient:
    def __init__(self, responses: list[object]) -> None:
        self.completions = FakeCompletions(responses)
        self.chat = SimpleNamespace(completions=self.completions)


def llm_with(monkeypatch: pytest.MonkeyPatch, responses: list[object]) -> tuple[DoubaoLLMClient, FakeClient]:
    monkeypatch.setenv("ARK_API_KEY", "test-key-never-logged")
    monkeypatch.setenv("DOUBAO_BASE_URL", "https://example.invalid/api/v3")
    monkeypatch.setenv("DOUBAO_MODEL", "test-model")
    fake = FakeClient(responses)
    return DoubaoLLMClient(client=fake), fake


def recipe_data(title: str = "AI 番茄鸡蛋面") -> dict:
    return {
        "title": title, "servings": 1, "estimated_minutes": 15, "difficulty": "简单",
        "ingredients": [{"name": "面条", "amount": 100, "unit": "克", "optional": False}, {"name": "番茄", "amount": 1, "unit": "个", "optional": False}, {"name": "鸡蛋", "amount": 1, "unit": "个", "optional": False}],
        "equipment": ["小锅"], "safety_notes": ["注意沸水和蒸汽"],
        "steps": [{"step_number": 7, "instruction": "锅中加水烧开后放入面条。", "duration_seconds": None, "heat_level": "大火", "safety_note": "注意沸水。"}, {"step_number": 9, "instruction": "加入番茄煮软后关火。", "duration_seconds": 180, "heat_level": "中火", "safety_note": None}],
        "source_url": "https://invented.example/not-allowed",
    }


def candidate_payload() -> str:
    return json.dumps({"candidates": [{
        "title": "AI 番茄鸡蛋面", "summary": "利用番茄鸡蛋和面条的快手面。",
        "estimated_minutes": 15, "difficulty": "简单",
        "main_ingredients": ["番茄", "鸡蛋", "面条"], "missing_ingredients": [],
        "match_reason": "能利用现有食材。",
        "recipe": recipe_data(),
    }]}, ensure_ascii=False)


def three_candidate_payload() -> str:
    candidates = []
    for index, title in enumerate(("番茄鸡蛋面", "家常番茄鸡蛋面", "快手番茄鸡蛋面"), start=1):
        candidates.append({
            "title": title, "summary": f"第{index}种做法。", "estimated_minutes": 15 + index,
            "difficulty": "简单", "main_ingredients": ["番茄", "鸡蛋", "面条"],
            "missing_ingredients": [], "match_reason": "符合需求。", "recipe": recipe_data(title),
        })
    return json.dumps({"candidates": candidates}, ensure_ascii=False)


def test_client_reads_only_environment_config_and_handles_text(monkeypatch: pytest.MonkeyPatch) -> None:
    llm, fake = llm_with(monkeypatch, ["普通回复"])
    assert llm.is_available()
    assert llm.base_url == "https://example.invalid/api/v3"
    assert llm.model == "test-model"
    assert llm.chat([{"role": "user", "content": "你好"}]) == "普通回复"
    assert fake.completions.calls[0]["model"] == "test-model"
    assert "test-key-never-logged" not in repr(llm.chat)


def test_client_uses_short_interactive_timeout_and_allows_environment_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARK_API_KEY", "test-key-never-logged")
    monkeypatch.delenv("DOUBAO_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("DOUBAO_MAX_RETRIES", raising=False)
    default_client = DoubaoLLMClient(client=FakeClient(["ok"]))
    assert default_client.timeout == DEFAULT_TIMEOUT_SECONDS == 90.0
    assert default_client.max_retries == DEFAULT_MAX_RETRIES == 0

    monkeypatch.setenv("DOUBAO_TIMEOUT_SECONDS", "7.5")
    monkeypatch.setenv("DOUBAO_MAX_RETRIES", "1")
    overridden = DoubaoLLMClient(client=FakeClient(["ok"]))
    assert overridden.timeout == 7.5
    assert overridden.max_retries == 1

    monkeypatch.setenv("DOUBAO_TIMEOUT_SECONDS", "invalid")
    monkeypatch.setenv("DOUBAO_MAX_RETRIES", "-1")
    fallback = DoubaoConfig.from_environment()
    assert fallback.timeout == DEFAULT_TIMEOUT_SECONDS
    assert fallback.max_retries == DEFAULT_MAX_RETRIES


def test_client_unavailable_and_failures_are_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ARK_API_KEY", raising=False)
    assert not DoubaoLLMClient(client=FakeClient([])).is_available()
    with pytest.raises(DoubaoClientError):
        DoubaoLLMClient(client=FakeClient([])).chat([{"role": "user", "content": "x"}])
    llm, _ = llm_with(monkeypatch, [TimeoutError("timeout")])
    with pytest.raises(DoubaoClientError):
        llm.chat([{"role": "user", "content": "x"}])
    for response in ("empty_choices", "empty_content"):
        llm, _ = llm_with(monkeypatch, [response])
        with pytest.raises(DoubaoClientError):
            llm.chat([{"role": "user", "content": "x"}])


def test_json_parser_accepts_code_fence_and_repairs_once(monkeypatch: pytest.MonkeyPatch) -> None:
    assert parse_json_response("```json\n{\"ok\": true}\n```") == {"ok": True}
    llm, fake = llm_with(monkeypatch, ["not json", "{\"fixed\": true}"])
    assert llm.generate_json([{"role": "user", "content": "x"}]) == {"fixed": True}
    assert len(fake.completions.calls) == 2
    llm, _ = llm_with(monkeypatch, ["not json", "still not json"])
    with pytest.raises(DoubaoClientError):
        llm.generate_json([{"role": "user", "content": "x"}])


def test_recipe_prompt_requires_serving_quantities_and_detailed_cooking_order() -> None:
    system_prompt = recipe_messages({"title": "番茄炒蛋"}, {"servings": 1})[0]["content"]
    assert "鸡蛋" in system_prompt
    assert "热锅" in system_prompt
    assert "生抽" in system_prompt
    assert "steak_thickness_cm" not in system_prompt

    bundle_prompt = recipe_bundle_messages({"requested_dish": "番茄炒蛋", "servings": 1})[0]["content"]
    assert "一次生成" in bundle_prompt
    assert "每个candidate都必须有recipe" in bundle_prompt
    assert "只生成1个完整候选" in bundle_prompt
    assert "适量" in bundle_prompt
    assert len(bundle_prompt) < 1450

    pantry_prompt = recipe_bundle_messages({"available_ingredients": ["番茄", "鸡蛋"]})[0]["content"]
    assert "生成1至3个不同候选" in pantry_prompt


def test_prompts_compact_payload_and_only_add_relevant_dish_rules() -> None:
    request = {
        "requested_dish": "宫保鸡丁", "servings": 1,
        "available_ingredients": [], "dietary_restrictions": [],
        "steak_thickness_cm": None,
    }
    candidate = {
        "candidate_id": "ai_宫保鸡丁_1", "title": "宫保鸡丁", "summary": "家常做法",
        "estimated_minutes": 20, "difficulty": "简单", "main_ingredients": ["鸡肉", "花生"],
        "source_name": "不应发送给模型", "source_url": None, "match_reason": "不需要重复发送",
    }

    candidate_prompt = candidate_messages(request)
    recipe_prompt = recipe_messages(candidate, request)
    candidate_payload = json.loads(candidate_prompt[1]["content"])
    recipe_payload = json.loads(recipe_prompt[1]["content"])

    assert candidate_payload == {"requested_dish": "宫保鸡丁", "servings": 1}
    assert "candidate_id" not in recipe_payload["candidate"]
    assert "source_name" not in recipe_payload["candidate"]
    assert "match_reason" not in recipe_payload["candidate"]
    assert "牛排必须" not in recipe_prompt[0]["content"]
    assert sum(len(message["content"]) for message in recipe_prompt) < 1400


def test_bundle_prompt_keeps_all_equipment_and_refresh_constraints() -> None:
    request = {
        "available_ingredients": ["鸡蛋"],
        "available_equipment": ["电饭煲"],
        "unavailable_equipment": ["炒锅", "烤箱"],
        "equipment_only": True,
        "excluded_candidate_ids": ["ai_旧菜_1"],
    }

    messages = recipe_bundle_messages(request)
    payload = json.loads(messages[1]["content"])

    assert payload == request
    assert "unavailable_equipment" in messages[0]["content"]
    assert "equipment_only=true" in messages[0]["content"]


def test_inventory_recommendation_prompts_require_every_stated_ingredient() -> None:
    request = {"available_ingredients": ["牛肉", "蘑菇"], "servings": 1}
    candidate_prompt = candidate_messages(request)[0]["content"]
    recipe_prompt = recipe_messages({"title": "蘑菇牛肉"}, request)[0]["content"]

    assert "牛肉、蘑菇" in candidate_prompt
    assert "不能忽略其中任何一种" in candidate_prompt
    assert "牛肉、蘑菇" in recipe_prompt
    assert "不可遗漏" in recipe_prompt


def test_steak_prompt_requires_complete_ingredients_and_avoids_generic_two_minute_sides() -> None:
    system_prompt = recipe_messages({"title": "煎牛排"}, {"servings": 1, "steak_thickness_cm": 2})[0]["content"]
    for ingredient in ("黑胡椒", "黄油", "橄榄油"):
        assert ingredient in system_prompt
    assert "120 秒" in system_prompt


def test_ai_provider_generates_candidates_and_normalized_recipe(monkeypatch: pytest.MonkeyPatch) -> None:
    llm, fake = llm_with(monkeypatch, [candidate_payload()])
    fallback = MockRecipeSearchProvider(SKILL_ROOT / "recipes")
    provider = DoubaoAIRecipeProvider(llm, fallback)
    request = RecipeSearchRequest(available_ingredients=["番茄", "鸡蛋", "面条"], servings=1, taste_preferences=["少盐"])
    candidates = provider.search_recipes(request)
    assert len(candidates) == 1
    assert candidates[0].source_name == "豆包 AI 生成"
    assert provider.get_recipe_detail(candidates[0])["name"] == "AI 番茄鸡蛋面"
    assert len(fake.completions.calls) == 1


def test_ai_provider_bypasses_persisted_cache_for_explicit_fresh_search(monkeypatch: pytest.MonkeyPatch) -> None:
    llm, fake = llm_with(monkeypatch, [candidate_payload()])

    class CachedFallback:
        def __init__(self) -> None:
            self.calls = 0

        def search_cached_recipes(self, request):
            self.calls += 1
            return [RecipeCandidate(
                candidate_id="cached-old",
                title="旧缓存菜谱",
                source_name="已保存菜谱",
                source_url=None,
                summary="旧结果",
                estimated_minutes=10,
                difficulty="简单",
                main_ingredients=["番茄", "鸡蛋", "面条"],
                main_seasonings=["盐"],
                missing_ingredients=[],
                match_reason="cache",
            )]

    fallback = CachedFallback()
    provider = DoubaoAIRecipeProvider(llm, fallback)
    request = RecipeSearchRequest(
        available_ingredients=["番茄", "鸡蛋", "面条"],
        servings=1,
        bypass_cache=True,
    )

    candidates = provider.search_recipes(request)

    assert fallback.calls == 0
    assert candidates[0].source_name == "豆包 AI 生成"
    assert len(fake.completions.calls) == 1
    assert candidates[0].source_url is None
    raw = provider.get_recipe_detail(candidates[0])
    assert raw["source_name"] == "豆包 AI 生成" and raw["source_url"] is None
    assert [step["step_number"] for step in raw["steps"]] == [1, 2]


def test_generated_recipe_is_persisted_and_reused_without_a_second_llm_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    generated_dir = tmp_path / "generated"
    llm, first_client = llm_with(monkeypatch, [candidate_payload()])
    fallback = MockRecipeSearchProvider(SKILL_ROOT / "recipes", generated_dir=generated_dir)
    provider = DoubaoAIRecipeProvider(llm, fallback)
    request = RecipeSearchRequest(
        requested_dish="番茄鸡蛋面", servings=1, taste_preferences=["少盐"],
    )

    candidates = provider.search_recipes(request)
    provider.get_recipe_detail(candidates[0])
    assert len(first_client.completions.calls) == 1
    assert len(list(generated_dir.glob("cached_*.json"))) == 1

    second_llm, second_client = llm_with(monkeypatch, [])
    reloaded_fallback = MockRecipeSearchProvider(SKILL_ROOT / "recipes", generated_dir=generated_dir)
    reloaded_provider = DoubaoAIRecipeProvider(second_llm, reloaded_fallback)
    cached = reloaded_provider.search_recipes(request)
    assert cached and reloaded_provider.mode == "local_cache"
    detail = reloaded_provider.get_recipe_detail(cached[0])
    assert detail["source_name"] == "已保存菜谱"
    assert second_client.completions.calls == []

    session = KitchenSession(recipe_provider=reloaded_provider)
    session.handle("我想做番茄鸡蛋面")
    session.handle("两个人")
    local_candidates = session.handle("少盐")
    assert local_candidates["provider_mode"] == "local_cache"
    assert local_candidates["steps"][0]["display"] == "已找到菜谱"
    assert "本地缓存" not in str(local_candidates)
    selected = session.handle("第一个")
    assert "本地缓存" not in str(selected)
    started = session.handle("开始")
    assert started["kitchen_state"] == COOKING
    assert "本地缓存" not in str(started)
    assert session.current_recipe["ingredients"][0]["amount"] == "200"
    assert second_client.completions.calls == []


def test_semantically_invalid_bundled_recipe_is_not_exposed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    invalid_payload = json.loads(candidate_payload())
    invalid_payload["candidates"][0]["recipe"]["ingredients"][0]["amount"] = "适量"
    llm, fake = llm_with(monkeypatch, [json.dumps(invalid_payload, ensure_ascii=False)])
    provider = DoubaoAIRecipeProvider(
        llm,
        MockRecipeSearchProvider(SKILL_ROOT / "recipes", generated_dir=tmp_path / "generated"),
    )

    with pytest.raises(AIRecipeProviderError):
        provider.search_recipes(RecipeSearchRequest(
            requested_dish="番茄鸡蛋面", servings=1, taste_preferences=["正常"],
        ))

    assert len(fake.completions.calls) == 1
    assert not list((tmp_path / "generated").glob("cached_*.json"))


def test_complete_recipe_rechecks_dietary_restrictions_before_cache(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    unsafe_payload = json.loads(candidate_payload())
    unsafe_payload["candidates"][0]["recipe"]["ingredients"].append({
        "name": "花生",
        "amount": 10,
        "unit": "克",
        "optional": False,
    })
    llm, fake = llm_with(monkeypatch, [json.dumps(unsafe_payload, ensure_ascii=False)])
    provider = DoubaoAIRecipeProvider(
        llm,
        MockRecipeSearchProvider(SKILL_ROOT / "recipes", generated_dir=tmp_path / "generated"),
    )

    with pytest.raises(AIRecipeProviderError):
        provider.search_recipes(RecipeSearchRequest(
            requested_dish="番茄鸡蛋面",
            servings=1,
            dietary_restrictions=["花生"],
        ))

    assert len(fake.completions.calls) == 1
    assert not list((tmp_path / "generated").glob("cached_*.json"))


def test_candidate_time_limit_and_excluded_titles_are_hard_constraints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm, _ = llm_with(monkeypatch, [candidate_payload()])
    provider = DoubaoAIRecipeProvider(llm, MockRecipeSearchProvider(SKILL_ROOT / "recipes"))
    with pytest.raises(AIRecipeProviderError):
        provider.search_recipes(RecipeSearchRequest(
            requested_dish="番茄鸡蛋面",
            servings=1,
            max_cooking_minutes=10,
        ))

    llm, _ = llm_with(monkeypatch, [candidate_payload()])
    provider = DoubaoAIRecipeProvider(llm, MockRecipeSearchProvider(SKILL_ROOT / "recipes"))
    with pytest.raises(AIRecipeProviderError):
        provider.search_recipes(RecipeSearchRequest(
            requested_dish="番茄鸡蛋面",
            servings=1,
            excluded_candidate_ids=["ai_AI_番茄鸡蛋面_1"],
        ))

def test_selected_recipe_uses_preloaded_detail_then_scales_persisted_detail(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    generated_dir = tmp_path / "generated"
    llm, first_client = llm_with(monkeypatch, [candidate_payload()])
    provider = DoubaoAIRecipeProvider(
        llm,
        MockRecipeSearchProvider(SKILL_ROOT / "recipes", generated_dir=generated_dir),
    )
    session = KitchenSession(recipe_provider=provider)

    session.handle("我想做番茄鸡蛋面")
    session.handle("一个人")
    candidates = session.handle("少盐")
    assert candidates["provider_mode"] == "ai_generated"
    assert len(list(generated_dir.glob("cached_*.json"))) == 1
    assert len(first_client.completions.calls) == 1

    session.handle("第一个")
    started = session.handle("好")
    assert started["kitchen_state"] == COOKING
    assert len(list(generated_dir.glob("cached_*.json"))) == 1
    assert len(first_client.completions.calls) == 1

    second_llm, second_client = llm_with(monkeypatch, [])
    second_provider = DoubaoAIRecipeProvider(
        second_llm,
        MockRecipeSearchProvider(SKILL_ROOT / "recipes", generated_dir=generated_dir),
    )
    second_session = KitchenSession(recipe_provider=second_provider)
    second_session.handle("我想做番茄鸡蛋面")
    second_session.handle("两个人")
    reused = second_session.handle("少盐")
    assert reused["provider_mode"] == "local_cache"

    second_session.handle("第一个")
    second_session.handle("好")
    assert second_session.current_recipe["servings"] == 2
    assert second_session.current_recipe["ingredients"][0]["amount"] == "200"
    assert second_client.completions.calls == []


def test_generated_pantry_recommendations_are_reused_by_ingredient_keywords(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    generated_dir = tmp_path / "generated"
    request = RecipeSearchRequest(
        available_ingredients=["番茄", "鸡蛋", "面条"], servings=1, taste_preferences=["少盐"],
    )
    first_llm, _ = llm_with(monkeypatch, [candidate_payload()])
    first_provider = DoubaoAIRecipeProvider(
        first_llm, MockRecipeSearchProvider(SKILL_ROOT / "recipes", generated_dir=generated_dir),
    )
    assert first_provider.search_recipes(request)

    second_llm, second_client = llm_with(monkeypatch, [])
    second_provider = DoubaoAIRecipeProvider(
        second_llm, MockRecipeSearchProvider(SKILL_ROOT / "recipes", generated_dir=generated_dir),
    )
    cached = second_provider.search_recipes(request)

    assert cached and second_provider.mode == "local_cache"
    assert all({"番茄", "鸡蛋", "面条"} <= set(candidate.main_ingredients) for candidate in cached)
    assert second_client.completions.calls == []


def test_outdated_generated_caches_are_removed_on_provider_startup(tmp_path: Path) -> None:
    generated_dir = tmp_path / "generated"
    generated_dir.mkdir()
    legacy = generated_dir / "cached_00c8598b4fde391d.json"
    legacy.write_text(json.dumps({
        "recipe_id": "cached_00c8598b4fde391d",
        "name": "旧糖醋排骨",
        "ingredients": [{"name": "排骨", "amount": "200克"}],
        "steps": [{"instruction": "洗净"}],
    }, ensure_ascii=False), encoding="utf-8")
    bundle = generated_dir / "cached_糖醋排骨.json"
    bundle.write_text(json.dumps({"cache_version": 2, "recipes": []}, ensure_ascii=False), encoding="utf-8")
    MockRecipeSearchProvider(SKILL_ROOT / "recipes", generated_dir=generated_dir)
    assert not legacy.exists()
    assert not bundle.exists()

    current_bundle = generated_dir / "cached_新糖醋排骨.json"
    current_bundle.write_text(json.dumps({"cache_version": MockRecipeSearchProvider.CACHE_VERSION, "recipes": []}, ensure_ascii=False), encoding="utf-8")
    MockRecipeSearchProvider(SKILL_ROOT / "recipes", generated_dir=generated_dir)
    assert current_bundle.exists()


def test_named_dish_returns_one_complete_recipe_in_one_call(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    generated_dir = tmp_path / "generated"
    llm, fake = llm_with(monkeypatch, [three_candidate_payload()])
    fallback = MockRecipeSearchProvider(SKILL_ROOT / "recipes", generated_dir=generated_dir)
    provider = DoubaoAIRecipeProvider(llm, fallback)
    candidates = provider.search_recipes(RecipeSearchRequest(
        requested_dish="番茄鸡蛋面", servings=1, taste_preferences=["少盐"],
    ))

    assert len(candidates) == 1
    assert len(fake.completions.calls) == 1
    cache_files = list(generated_dir.glob("cached_*.json"))
    assert len(cache_files) == 1
    bundle = json.loads(cache_files[0].read_text(encoding="utf-8"))
    assert len(bundle["recipes"]) == 1
    assert cache_files[0].name.startswith("cached_番茄鸡蛋面")
    before = len(fake.completions.calls)
    assert provider.get_recipe_detail(candidates[0])["name"] == "番茄鸡蛋面"
    assert len(fake.completions.calls) == before

    reloaded = MockRecipeSearchProvider(SKILL_ROOT / "recipes", generated_dir=generated_dir)
    cached = reloaded.search_cached_recipes(RecipeSearchRequest(
        requested_dish="番茄鸡蛋面", servings=1, taste_preferences=["少盐"],
    ))
    assert len(cached) == 1


def test_pantry_request_can_return_three_preloaded_details_in_one_call(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    generated_dir = tmp_path / "generated"
    llm, fake = llm_with(monkeypatch, [three_candidate_payload()])
    provider = DoubaoAIRecipeProvider(
        llm,
        MockRecipeSearchProvider(SKILL_ROOT / "recipes", generated_dir=generated_dir),
    )

    candidates = provider.search_recipes(RecipeSearchRequest(
        available_ingredients=["番茄", "鸡蛋", "面条"],
        servings=1,
        taste_preferences=["少盐"],
    ))

    assert len(candidates) == 3
    assert len(fake.completions.calls) == 1
    assert len(list(generated_dir.glob("cached_*.json"))) == 3
    assert provider.get_recipe_detail(candidates[2])["name"] == "快手番茄鸡蛋面"


def test_session_uses_ai_then_waits_for_confirmation(monkeypatch: pytest.MonkeyPatch) -> None:
    llm, fake = llm_with(monkeypatch, [candidate_payload()])
    fallback = MockRecipeSearchProvider(SKILL_ROOT / "recipes")
    session = KitchenSession(recipe_provider=DoubaoAIRecipeProvider(llm, fallback))
    session.handle("我想做番茄鸡蛋面")
    session.handle("一个人")
    candidates = session.handle("少盐")
    assert candidates["kitchen_state"] == PRESENTING_CANDIDATES
    assert candidates["provider_mode"] == "ai_generated"
    selected = session.handle("第一个")
    assert selected["kitchen_state"] == WAITING_RECIPE_CONFIRMATION
    assert session.current_recipe is None
    cooking = session.handle("开始吧")
    assert cooking["kitchen_state"] == COOKING
    assert cooking["provider_mode"] == "ai_generated"
    assert session.current_recipe["source_name"] == "豆包 AI 生成"
    assert session.current_recipe["source_url"] is None
    assert all(step["robot_action"] for step in session.current_recipe["steps"])
    assert "豆包 AI 生成" not in str(candidates)
    assert "豆包 AI 生成" not in str(selected)
    assert "豆包 AI 生成" not in str(cooking)
    assert len(fake.completions.calls) == 1


def test_ai_display_uses_user_facing_generation_copy_without_provider_brand(monkeypatch: pytest.MonkeyPatch) -> None:
    llm, _ = llm_with(monkeypatch, [candidate_payload()])
    session = KitchenSession(recipe_provider=DoubaoAIRecipeProvider(llm, MockRecipeSearchProvider(SKILL_ROOT / "recipes")))
    session.handle("我想做番茄鸡蛋面")
    session.handle("一个人")
    candidates = session.handle("少盐")
    displays = "\n".join(step["display"] for step in candidates["steps"])
    assert "我正在为你生成菜谱建议" in displays
    assert "我为你生成的菜谱" in displays
    assert "豆包 AI" not in displays
    selected = session.handle("第一个")
    assert "生成方式：根据你的需求生成" in selected["display"]
    assert "豆包 AI" not in selected["display"]


def test_ai_provider_rejects_candidates_that_replace_requested_dish(monkeypatch: pytest.MonkeyPatch) -> None:
    llm, _ = llm_with(monkeypatch, [candidate_payload()])
    provider = DoubaoAIRecipeProvider(llm, MockRecipeSearchProvider(SKILL_ROOT / "recipes"))
    with pytest.raises(AIRecipeProviderError):
        provider.search_recipes(RecipeSearchRequest(requested_dish="番茄肥牛", servings=1, taste_preferences=["正常口味"]))

    matching = json.dumps({"candidates": [{
        "title": "番茄肥牛", "summary": "酸甜开胃的快手肥牛做法。", "estimated_minutes": 15,
        "difficulty": "简单", "main_ingredients": ["番茄", "肥牛"], "missing_ingredients": ["肥牛"],
        "match_reason": "保留你指定的番茄肥牛，并列出缺少的肥牛。",
        "recipe": {
            "title": "番茄肥牛", "servings": 1, "estimated_minutes": 15, "difficulty": "简单",
            "ingredients": [
                {"name": "番茄", "amount": 1, "unit": "个", "optional": False},
                {"name": "肥牛", "amount": 150, "unit": "克", "optional": False},
            ],
            "equipment": ["炒锅"], "safety_notes": [],
            "steps": [{"instruction": "放入150克肥牛和1个番茄煮熟。", "duration_seconds": 180}],
        },
    }]}, ensure_ascii=False)
    llm, _ = llm_with(monkeypatch, [matching])
    candidates = DoubaoAIRecipeProvider(llm, MockRecipeSearchProvider(SKILL_ROOT / "recipes")).search_recipes(
        RecipeSearchRequest(requested_dish="番茄肥牛", servings=1, taste_preferences=["正常口味"])
    )
    assert candidates[0].title == "番茄肥牛"
    assert candidates[0].missing_ingredients == []


def test_ai_provider_bounds_standard_steak_sear_time_and_keeps_it_as_a_reference(monkeypatch: pytest.MonkeyPatch) -> None:
    candidate_data = {
        "title": "煎牛排", "summary": "适合新手。", "estimated_minutes": 15, "difficulty": "简单",
        "main_ingredients": ["牛排", "橄榄油", "盐", "黑胡椒", "黄油"], "missing_ingredients": ["牛排"], "match_reason": "指定菜名。",
    }
    recipe = {
        "title": "煎牛排", "servings": 1, "estimated_minutes": 15, "difficulty": "简单",
        "ingredients": [{"name": "牛排", "amount": 1, "unit": "块", "optional": False}],
        "equipment": ["平底锅"], "steps": [
            {"instruction": "牛排正面煎 2 分钟。", "duration_seconds": 120, "heat_level": "中大火", "safety_note": None},
            {"instruction": "翻面后煎另一面 2 分钟。", "duration_seconds": 120, "heat_level": "中大火", "safety_note": None},
        ],
    }
    candidate_data["recipe"] = recipe
    bundled = json.dumps({"candidates": [candidate_data]}, ensure_ascii=False)
    llm, _ = llm_with(monkeypatch, [bundled])
    provider = DoubaoAIRecipeProvider(llm, MockRecipeSearchProvider(SKILL_ROOT / "recipes"))
    request = RecipeSearchRequest(requested_dish="煎牛排", servings=1, taste_preferences=["正常"], steak_doneness="五分熟", steak_thickness_cm=2.0)
    selected = provider.search_recipes(request)[0]
    raw = provider.get_recipe_detail(selected)
    assert selected.missing_ingredients == []
    assert [step["duration_seconds"] for step in raw["steps"]] == [60, 60]
    assert all("初始参考" in step["instruction"] and "温度计" in step["safety_note"] for step in raw["steps"])
    ingredient_names = {item["name"] for item in raw["ingredients"]}
    assert {"牛排", "精炼橄榄油", "黄油", "盐", "黑胡椒"} <= ingredient_names


def test_invalid_ai_dish_candidates_are_not_replaced_with_unrelated_mock_dishes(monkeypatch: pytest.MonkeyPatch) -> None:
    llm, _ = llm_with(monkeypatch, [candidate_payload()])
    session = KitchenSession(recipe_provider=DoubaoAIRecipeProvider(llm, MockRecipeSearchProvider(SKILL_ROOT / "recipes")))
    session.handle("我要做番茄肥牛")
    session.handle("一个人")
    response = session.handle("正常")
    assert response["provider_mode"] == "mock"
    assert response["recipe_candidates"] == []
    assert "暂无候选" in response["steps"][-1]["display"]


def test_ai_generated_feedback_reaches_all_mock_channels(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    llm, _ = llm_with(monkeypatch, [candidate_payload()])
    session = KitchenSession(recipe_provider=DoubaoAIRecipeProvider(llm, MockRecipeSearchProvider(SKILL_ROOT / "recipes")))
    session.handle("我想做番茄鸡蛋面")
    session.handle("一个人")
    candidates = session.handle("少盐")
    RuntimeExecutor(no_play=True).execute_plan(candidates)
    output = capsys.readouterr().out
    assert "[模拟SDK-语音请求]" in output
    assert "[模拟SDK-屏幕]" in output
    assert "[模拟SDK-动作]" in output
    assert "[模拟SDK-灯带]" in output
    assert "[模拟SDK-表情]" in output
    session.handle("第一个")
    cooking = session.handle("开始吧")
    RuntimeExecutor(no_play=True).execute_plan(cooking)
    assert "步骤 1" in capsys.readouterr().out


def test_ai_provider_bad_json_or_timeout_falls_back_to_mock(monkeypatch: pytest.MonkeyPatch) -> None:
    llm, _ = llm_with(monkeypatch, ["bad", "still bad"])
    fallback = MockRecipeSearchProvider(SKILL_ROOT / "recipes")
    session = KitchenSession(recipe_provider=DoubaoAIRecipeProvider(llm, fallback))
    session.handle("我想做番茄鸡蛋面")
    session.handle("一个人")
    response = session.handle("少盐")
    assert response["provider_mode"] == "mock"
    assert any("离线" in item.get("display", "") for item in response["steps"])


def test_invalid_ai_recipe_detail_keeps_selected_dish_and_offers_retry() -> None:
    fallback = MockRecipeSearchProvider(SKILL_ROOT / "recipes")

    class InvalidDetailProvider:
        mode = "ai_generated"

        def __init__(self, mock_provider) -> None:
            self.fallback = mock_provider

        def search_recipes(self, request):
            return fallback.search_recipes(request)

        def get_recipe_detail(self, candidate):
            return {"name": "无效菜谱", "ingredients": [], "steps": []}

        def fallback_recipe(self):
            return fallback.fallback_recipe()

    session = KitchenSession(recipe_provider=InvalidDetailProvider(fallback))
    session.handle("我想做番茄鸡蛋面")
    session.handle("一个人")
    session.handle("少盐")
    session.handle("第一个")
    response = session.handle("开始吧")
    assert response["kitchen_state"] == WAITING_RECIPE_CONFIRMATION
    assert response["provider_mode"] == "ai_generated"
    assert session.current_recipe is None
    assert "没有换成其他菜" in response["question"]


def test_confirmation_never_calls_model_after_bundled_generation(monkeypatch: pytest.MonkeyPatch) -> None:
    llm, fake = llm_with(monkeypatch, [
        candidate_payload(),
        TimeoutError("must remain unused"),
    ])
    fallback = MockRecipeSearchProvider(SKILL_ROOT / "recipes")
    session = KitchenSession(recipe_provider=DoubaoAIRecipeProvider(llm, fallback))
    session.handle("我想做番茄鸡蛋面")
    session.handle("一个人")
    session.handle("少盐")
    session.handle("第一个")
    result = session.handle("好")
    assert result["kitchen_state"] == COOKING
    assert result["provider_mode"] == "ai_generated"
    assert session.current_recipe is not None
    assert len(fake.completions.calls) == 1


def test_single_ai_candidate_accepts_good_without_selection_or_second_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm, fake = llm_with(monkeypatch, [candidate_payload()])
    session = KitchenSession(
        recipe_provider=DoubaoAIRecipeProvider(
            llm,
            MockRecipeSearchProvider(SKILL_ROOT / "recipes"),
        )
    )
    session.handle("我想做番茄鸡蛋面")
    session.handle("一个人")
    candidates = session.handle("少盐")
    assert len(candidates["recipe_candidates"]) == 1
    assert candidates["kitchen_state"] == PRESENTING_CANDIDATES

    result = session.handle("好")

    assert result["kitchen_state"] == COOKING
    assert session.selected_candidate == session.recipe_candidates[0]
    assert session.current_recipe is not None
    assert len(fake.completions.calls) == 1


def test_question_service_uses_local_safety_before_ai(monkeypatch: pytest.MonkeyPatch) -> None:
    llm, fake = llm_with(monkeypatch, ["普通问题的豆包回答。"])
    service = DoubaoCookingQuestionService(llm)
    context = CookingContext({"name": "测试菜"}, {"instruction": "加热", "heat_level": "中火"}, 1, [], [], [], [], None, [])
    safety = service.answer("锅里起火了", context)
    assert safety.should_pause_cooking and not fake.completions.calls
    ordinary = service.answer("汤怎样更浓一些？", context)
    assert ordinary.answer == "普通问题的豆包回答。"
    assert len(fake.completions.calls) == 1
