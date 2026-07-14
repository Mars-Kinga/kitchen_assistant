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
from kitchen.models import CookingContext, RecipeSearchRequest  # noqa: E402
from kitchen.session_store import KitchenSession  # noqa: E402
from kitchen.states import COOKING, PRESENTING_CANDIDATES, WAITING_RECIPE_CONFIRMATION  # noqa: E402
from llm.doubao_client import DoubaoClientError, DoubaoLLMClient, parse_json_response  # noqa: E402
from llm.prompts import candidate_messages, recipe_messages  # noqa: E402
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


def candidate_payload() -> str:
    return json.dumps({"candidates": [{
        "title": "AI 番茄鸡蛋面", "summary": "利用番茄鸡蛋和面条的快手面。",
        "estimated_minutes": 15, "difficulty": "简单",
        "main_ingredients": ["番茄", "鸡蛋", "面条"], "missing_ingredients": [],
        "match_reason": "能利用现有食材。",
    }]}, ensure_ascii=False)


def three_candidate_payload() -> str:
    candidates = []
    for index, title in enumerate(("番茄鸡蛋面", "家常番茄鸡蛋面", "快手番茄鸡蛋面"), start=1):
        candidates.append({
            "title": title, "summary": f"第{index}种做法。", "estimated_minutes": 15 + index,
            "difficulty": "简单", "main_ingredients": ["番茄", "鸡蛋", "面条"],
            "missing_ingredients": [], "match_reason": "符合需求。",
        })
    return json.dumps({"candidates": candidates}, ensure_ascii=False)


def recipe_payload() -> str:
    return json.dumps({
        "title": "AI 番茄鸡蛋面", "servings": 1, "estimated_minutes": 15, "difficulty": "简单",
        "ingredients": [{"name": "面条", "amount": 100, "unit": "克", "optional": False}, {"name": "番茄", "amount": 1, "unit": "个", "optional": False}],
        "equipment": ["小锅"], "safety_notes": ["注意沸水和蒸汽"],
        "steps": [{"step_number": 7, "instruction": "锅中加水烧开后放入面条。", "duration_seconds": None, "heat_level": "大火", "safety_note": "注意沸水。"}, {"step_number": 9, "instruction": "加入番茄煮软后关火。", "duration_seconds": 180, "heat_level": "中火", "safety_note": None}],
        "source_url": "https://invented.example/not-allowed",
    }, ensure_ascii=False)


def test_client_reads_only_environment_config_and_handles_text(monkeypatch: pytest.MonkeyPatch) -> None:
    llm, fake = llm_with(monkeypatch, ["普通回复"])
    assert llm.is_available()
    assert llm.base_url == "https://example.invalid/api/v3"
    assert llm.model == "test-model"
    assert llm.chat([{"role": "user", "content": "你好"}]) == "普通回复"
    assert fake.completions.calls[0]["model"] == "test-model"
    assert "test-key-never-logged" not in repr(llm.chat)


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


def test_steak_prompt_requires_complete_ingredients_and_avoids_generic_two_minute_sides() -> None:
    system_prompt = recipe_messages({"title": "煎牛排"}, {"servings": 1, "steak_thickness_cm": 2})[0]["content"]
    for ingredient in ("黑胡椒", "黄油", "橄榄油"):
        assert ingredient in system_prompt
    assert "120 秒" in system_prompt


def test_ai_provider_generates_candidates_and_normalized_recipe(monkeypatch: pytest.MonkeyPatch) -> None:
    llm, _ = llm_with(monkeypatch, [candidate_payload(), recipe_payload()])
    fallback = MockRecipeSearchProvider(SKILL_ROOT / "recipes")
    provider = DoubaoAIRecipeProvider(llm, fallback)
    request = RecipeSearchRequest(available_ingredients=["番茄", "鸡蛋", "面条"], servings=1, taste_preferences=["少盐"])
    candidates = provider.search_recipes(request)
    assert len(candidates) == 1
    assert candidates[0].source_name == "豆包 AI 生成"
    assert candidates[0].source_url is None
    raw = provider.get_recipe_detail(candidates[0])
    assert raw["source_name"] == "豆包 AI 生成" and raw["source_url"] is None
    assert [step["step_number"] for step in raw["steps"]] == [1, 2]


def test_generated_recipe_is_persisted_and_reused_without_a_second_llm_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    generated_dir = tmp_path / "generated"
    llm, first_client = llm_with(monkeypatch, [candidate_payload(), recipe_payload()])
    fallback = MockRecipeSearchProvider(SKILL_ROOT / "recipes", generated_dir=generated_dir)
    provider = DoubaoAIRecipeProvider(llm, fallback)
    request = RecipeSearchRequest(
        requested_dish="番茄鸡蛋面", servings=1, taste_preferences=["少盐"],
    )

    candidates = provider.search_recipes(request)
    provider.get_recipe_detail(candidates[0])
    assert len(first_client.completions.calls) == 2
    assert len(list(generated_dir.glob("cached_*.json"))) == 1

    second_llm, second_client = llm_with(monkeypatch, [])
    reloaded_fallback = MockRecipeSearchProvider(SKILL_ROOT / "recipes", generated_dir=generated_dir)
    reloaded_provider = DoubaoAIRecipeProvider(second_llm, reloaded_fallback)
    cached = reloaded_provider.search_recipes(request)
    assert cached and reloaded_provider.mode == "local_cache"
    detail = reloaded_provider.get_recipe_detail(cached[0])
    assert detail["source_name"] == "本地缓存菜谱"
    assert second_client.completions.calls == []

    session = KitchenSession(recipe_provider=reloaded_provider)
    session.handle("我想做番茄鸡蛋面")
    session.handle("一个人")
    local_candidates = session.handle("少盐")
    assert local_candidates["provider_mode"] == "local_cache"
    assert "本地菜谱缓存" in local_candidates["steps"][0]["display"]
    session.handle("第一个")
    assert session.handle("开始")["kitchen_state"] == COOKING
    assert second_client.completions.calls == []


def test_all_ai_candidates_are_warmed_and_persisted(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    generated_dir = tmp_path / "generated"
    llm, fake = llm_with(monkeypatch, [three_candidate_payload(), recipe_payload(), recipe_payload(), recipe_payload()])
    fallback = MockRecipeSearchProvider(SKILL_ROOT / "recipes", generated_dir=generated_dir)
    provider = DoubaoAIRecipeProvider(llm, fallback)
    candidates = provider.search_recipes(RecipeSearchRequest(
        requested_dish="番茄鸡蛋面", servings=1, taste_preferences=["少盐"],
    ))

    assert len(candidates) == 3
    assert len(fake.completions.calls) == 4  # one candidate call + three detail calls
    assert len(list(generated_dir.glob("cached_*.json"))) == 3
    before = len(fake.completions.calls)
    assert provider.get_recipe_detail(candidates[2])["name"] == "AI 番茄鸡蛋面"
    assert len(fake.completions.calls) == before

    reloaded = MockRecipeSearchProvider(SKILL_ROOT / "recipes", generated_dir=generated_dir)
    cached = reloaded.search_cached_recipes(RecipeSearchRequest(
        requested_dish="番茄鸡蛋面", servings=1, taste_preferences=["少盐"],
    ))
    assert len(cached) == 3


def test_session_uses_ai_then_waits_for_confirmation(monkeypatch: pytest.MonkeyPatch) -> None:
    llm, _ = llm_with(monkeypatch, [candidate_payload(), recipe_payload()])
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
    }]}, ensure_ascii=False)
    llm, _ = llm_with(monkeypatch, [matching])
    candidates = DoubaoAIRecipeProvider(llm, MockRecipeSearchProvider(SKILL_ROOT / "recipes")).search_recipes(
        RecipeSearchRequest(requested_dish="番茄肥牛", servings=1, taste_preferences=["正常口味"])
    )
    assert candidates[0].title == "番茄肥牛"
    assert candidates[0].missing_ingredients == []


def test_ai_provider_bounds_standard_steak_sear_time_and_keeps_it_as_a_reference(monkeypatch: pytest.MonkeyPatch) -> None:
    candidate = json.dumps({"candidates": [{
        "title": "煎牛排", "summary": "适合新手。", "estimated_minutes": 15, "difficulty": "简单",
        "main_ingredients": ["牛排", "橄榄油", "盐", "黑胡椒", "黄油"], "missing_ingredients": ["牛排"], "match_reason": "指定菜名。",
    }]}, ensure_ascii=False)
    recipe = json.dumps({
        "title": "煎牛排", "servings": 1, "estimated_minutes": 15, "difficulty": "简单",
        "ingredients": [{"name": "牛排", "amount": 1, "unit": "块", "optional": False}],
        "equipment": ["平底锅"], "steps": [
            {"instruction": "牛排正面煎 2 分钟。", "duration_seconds": 120, "heat_level": "中大火", "safety_note": None},
            {"instruction": "翻面后煎另一面 2 分钟。", "duration_seconds": 120, "heat_level": "中大火", "safety_note": None},
        ],
    }, ensure_ascii=False)
    llm, _ = llm_with(monkeypatch, [candidate, recipe])
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
    llm, _ = llm_with(monkeypatch, [candidate_payload(), recipe_payload()])
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


def test_real_ai_provider_invalid_detail_does_not_crash_session(monkeypatch: pytest.MonkeyPatch) -> None:
    llm, _ = llm_with(monkeypatch, [candidate_payload(), "{\"title\": \"缺步骤\", \"ingredients\": []}", "仍然不是 JSON"])
    fallback = MockRecipeSearchProvider(SKILL_ROOT / "recipes")
    session = KitchenSession(recipe_provider=DoubaoAIRecipeProvider(llm, fallback))
    session.handle("我想做番茄鸡蛋面")
    session.handle("一个人")
    session.handle("少盐")
    session.handle("第一个")
    result = session.handle("开始吧")
    assert result["kitchen_state"] == WAITING_RECIPE_CONFIRMATION
    assert result["provider_mode"] == "ai_generated"
    assert session.current_recipe is None
    assert "重试" in result["question"]


def test_question_service_uses_local_safety_before_ai(monkeypatch: pytest.MonkeyPatch) -> None:
    llm, fake = llm_with(monkeypatch, ["普通问题的豆包回答。"])
    service = DoubaoCookingQuestionService(llm)
    context = CookingContext({"name": "测试菜"}, {"instruction": "加热", "heat_level": "中火"}, 1, [], [], [], [], None, [])
    safety = service.answer("锅里起火了", context)
    assert safety.should_pause_cooking and not fake.completions.calls
    ordinary = service.answer("汤怎样更浓一些？", context)
    assert ordinary.answer == "普通问题的豆包回答。"
    assert len(fake.completions.calls) == 1
