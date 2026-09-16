from __future__ import annotations

import json
import sys
from pathlib import Path
from urllib.request import Request, urlopen

import pytest

from runtime_core.ingredient_vision import IngredientVisionService
from runtime_core.kitchen_console import KitchenConsoleServer, KitchenConsoleService


ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = ROOT / "skills" / "kitchen_assistant"
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

from kitchen.session_store import KitchenSession  # noqa: E402
from kitchen.models import RecipeCandidate  # noqa: E402
from providers.mock_recipe_provider import MockRecipeSearchProvider  # noqa: E402


class FakeCamera:
    def capture_data_url(self) -> str:
        return "data:image/jpeg;base64,AA=="


class PantryVisionClient:
    def is_available(self) -> bool:
        return True

    def vision_json(self, image_data_url: str, prompt: str):
        assert image_data_url.startswith("data:image/jpeg")
        assert "不提供" in prompt or "不要给事故处置建议" in prompt
        return {
            "ingredients": [
                {"name": "番茄", "confidence": "高"},
                {"name": "鸡蛋", "confidence": "中"},
                {"name": "番茄", "confidence": "高"},
            ],
            "uncertain_items": ["被遮挡的包装袋"],
            "summary": "看到了番茄和鸡蛋。",
            "needs_retake": False,
            "retake_instruction": None,
        }


class FakeManager:
    active_skill_name = None

    def call_skill_hook(self, _skill, hook, *args, **kwargs):
        if hook == "status_snapshot":
            return {
                "state": "IDLE", "session_active": False, "recipe": None,
                "current_step": None, "timer": None, "candidates": [],
                "confirmed_ingredients": [], "provider_mode": "mock",
            }
        if hook == "recommend_from_ingredients":
            self.active_skill_name = "kitchen_assistant"
            return {
                "route": "skill_result", "task_name": "AI 厨房助手",
                "kitchen_state": "PRESENTING_CANDIDATES", "session_active": True,
                "provider_mode": "mock", "speech": "找到一个方案",
                "display": "推荐菜谱", "robot_action": "nod", "led_effect": "green",
                "expression": "happy", "recipe_candidates": [],
            }
        if hook == "browse_local_recipes":
            return [{"recipe_id": "tomato_egg", "name": "番茄炒蛋"}]
        if hook == "console_recipe_detail":
            return {"recipe_id": args[0], "name": "番茄炒蛋", "ingredients": [], "steps": []}
        if hook == "select_console_recipe":
            return {
                "route": "skill_result", "task_name": "AI 厨房助手",
                "kitchen_state": "COOKING", "session_active": True,
                "provider_mode": "mock", "speech": "开始做菜",
                "display": "开始做菜", "robot_action": "nod", "led_effect": "green",
                "expression": "happy",
            }
        if hook == "navigate_console_step":
            return {
                "route": "skill_result", "task_name": "AI 厨房助手",
                "kitchen_state": "COOKING", "session_active": True,
                "provider_mode": "mock", "speech": "下一步：翻炒两分钟。",
                "display": "步骤 2/3：翻炒两分钟",
                "robot_action": "encourage_gesture", "led_effect": "green_dynamic",
                "expression": "happy", "current_step": 2,
            }
        raise AssertionError(hook)


class FakeExecutor:
    def __init__(self):
        self.plans = []

    def execute_plan(self, plan):
        self.plans.append(plan)
        return {"status": "ok"}

    def status_snapshot(self):
        return {
            "state": "idle", "action": "idle_wait", "display": "等待任务",
            "updated_at": 0, "simulated": True,
        }


def test_pantry_photo_result_is_editable_and_deduplicated() -> None:
    service = IngredientVisionService(FakeCamera(), PantryVisionClient())

    result = service.analyze_pantry_image("data:image/jpeg;base64,AA==")

    assert [item["name"] for item in result["ingredients"]] == ["番茄", "鸡蛋"]
    assert result["uncertain_items"] == ["被遮挡的包装袋"]
    assert service.capture_preview().startswith("data:image/jpeg")
    assert service.status_snapshot()["state"] == "ready"


def test_console_recommendation_starts_shared_kitchen_session() -> None:
    provider = MockRecipeSearchProvider(ROOT / "skills" / "kitchen_assistant" / "recipes")
    session = KitchenSession(recipe_provider=provider)

    result = session.recommend_from_ingredients(["西红柿", "鸡蛋"], servings=2, taste="正常")
    snapshot = session.status_snapshot()

    assert result["kitchen_state"] == "PRESENTING_CANDIDATES"
    assert result["session_active"] is True
    assert snapshot["confirmed_ingredients"] == ["番茄", "鸡蛋"]
    assert snapshot["candidates"]


def test_console_step_control_executes_robot_feedback_and_prints_reply(capsys) -> None:
    executor = FakeExecutor()
    service = KitchenConsoleService(
        FakeManager(), executor, IngredientVisionService(FakeCamera(), PantryVisionClient()),
    )

    result = service.navigate_step("next")

    assert executor.plans[-1]["robot_action"] == "encourage_gesture"
    assert result["message"] == "步骤 2/3：翻炒两分钟"
    terminal = capsys.readouterr().out
    assert "[局域网控制台] 下一步" in terminal
    assert "[机器人回复] 下一步：翻炒两分钟。" in terminal


def test_console_can_browse_open_and_select_local_recipe() -> None:
    provider = MockRecipeSearchProvider(ROOT / "skills" / "kitchen_assistant" / "recipes")
    session = KitchenSession(recipe_provider=provider)

    rows = session.browse_local_recipes("番茄")
    assert any(row["name"] == "番茄炒蛋" for row in rows)
    recipe_id = next(row["recipe_id"] for row in rows if row["name"] == "番茄炒蛋")
    detail = session.console_recipe_detail(recipe_id, servings=2)
    assert detail["ingredients"] and detail["steps"]

    selected = session.select_console_recipe(recipe_id, servings=2)
    assert selected["kitchen_state"] == "COOKING"
    assert session.current_recipe["name"] == "番茄炒蛋"

    total = len(session.current_recipe["steps"])
    next_result = session.navigate_console_step("next")
    assert next_result["current_step"] == min(2, total)
    assert next_result.get("speech") or next_result.get("steps")
    previous_result = session.navigate_console_step("previous")
    assert previous_result["current_step"] == 1
    assert previous_result.get("speech") or previous_result.get("steps")


def test_local_meat_recipe_confirms_before_first_step_and_cancel_cannot_advance():
    session = KitchenSession()
    recipe_id = next(row["recipe_id"] for row in session.browse_local_recipes("咖喱肥牛") if row["name"] == "咖喱肥牛")
    selected = session.select_console_recipe(recipe_id, servings=1)
    assert selected["kitchen_state"] == "WAITING_MEAT_THAW"
    for direction in ("previous", "next"):
        with pytest.raises(ValueError, match="确认.*食材"):
            session.navigate_console_step(direction)
    assert session.step_index == 0
    started = session.console_cooking_action("fresh_ingredients")
    assert started["kitchen_state"] == "COOKING"
    assert session.status_snapshot()["current_step"]["number"] == 1
    session.navigate_console_step("next")
    assert session.step_index == 1
    cancelled = session.console_cooking_action("cancel_task")
    assert cancelled["kitchen_state"] == "CANCELLED"
    snapshot = session.status_snapshot()
    assert snapshot["session_active"] is False
    assert snapshot["recipe"] is None
    assert snapshot["current_step"] is None
    assert snapshot["timer"] is None
    for direction in ("previous", "next"):
        with pytest.raises(ValueError):
            session.navigate_console_step(direction)
    other = next(row["recipe_id"] for row in session.browse_local_recipes("番茄炒蛋") if row["name"] == "番茄炒蛋")
    assert session.select_console_recipe(other)["kitchen_state"] == "COOKING"


def test_cancel_while_waiting_for_ingredients_clears_recipe_instead_of_unlocking_next():
    session = KitchenSession()
    recipe_id = next(row["recipe_id"] for row in session.browse_local_recipes("咖喱肥牛") if row["name"] == "咖喱肥牛")
    session.select_console_recipe(recipe_id)
    session.console_cooking_action("cancel_task")
    assert session.status_snapshot()["recipe"] is None
    with pytest.raises(ValueError):
        session.navigate_console_step("next")
    with pytest.raises(ValueError):
        session.console_cooking_action("fresh_ingredients")


def test_local_paused_recipe_cannot_bypass_pause_by_switching_steps():
    session = KitchenSession()
    recipe_id = next(row["recipe_id"] for row in session.browse_local_recipes("番茄炒蛋") if row["name"] == "番茄炒蛋")
    session.select_console_recipe(recipe_id)
    session.console_cooking_action("pause")
    for direction in ("previous", "next"):
        with pytest.raises(ValueError, match="恢复"):
            session.navigate_console_step(direction)
    assert session.step_index == 0
    session.console_cooking_action("resume")
    session.navigate_console_step("next")
    assert session.step_index == 1


def test_console_navigation_auto_starts_timer_for_timed_step() -> None:
    session = KitchenSession(clock=lambda: 100.0)
    session.current_recipe = session.normalizer.normalize({
        "name": "控制台计时测试菜",
        "ingredients": [{"name": "番茄", "amount": 1, "unit": "个"}],
        "steps": [
            {"instruction": "番茄洗净切块。"},
            {"instruction": "放入番茄中火翻炒 2 分钟。", "duration_seconds": 120},
            {"instruction": "关火装盘。"},
        ],
    })
    session.state = "COOKING"
    session.step_index = 0

    session.navigate_console_step("next")
    timed = session.status_snapshot()

    assert timed["current_step"]["number"] == 2
    assert timed["timer"] == {
        "label": "第 2 步",
        "remaining_seconds": 120,
        "duration_seconds": 120,
        "state": "running",
    }

    session.navigate_console_step("next")
    untimed = session.status_snapshot()

    assert untimed["current_step"]["number"] == 3
    assert untimed["timer"] is None

    session.navigate_console_step("previous")
    restarted = session.status_snapshot()

    assert restarted["current_step"]["number"] == 2
    assert restarted["timer"]["remaining_seconds"] == 120


def test_console_cooking_auto_starts_timer_when_first_step_is_timed() -> None:
    session = KitchenSession(clock=lambda: 100.0)
    session.current_recipe = session.normalizer.normalize({
        "name": "第一步计时测试菜",
        "ingredients": [{"name": "鸡蛋", "amount": 2, "unit": "个"}],
        "steps": [
            {"instruction": "鸡蛋放入水中煮 8 分钟。", "duration_seconds": 480},
            {"instruction": "捞出鸡蛋后装盘。"},
        ],
    })

    started = session._begin_cooking()
    snapshot = session.status_snapshot()

    assert started["kitchen_state"] == "COOKING"
    assert snapshot["current_step"]["number"] == 1
    assert snapshot["timer"]["remaining_seconds"] == 480
    assert snapshot["timer"]["label"] == "第 1 步"


def test_console_last_step_completes_with_robot_encouragement() -> None:
    session = KitchenSession(clock=lambda: 100.0)
    session.current_recipe = session.normalizer.normalize({
        "name": "完成测试菜",
        "ingredients": [{"name": "番茄", "amount": 1, "unit": "个"}],
        "steps": [
            {"instruction": "番茄切块。"},
            {"instruction": "关火装盘。"},
        ],
    })
    session.state = "COOKING"
    session.step_index = 1

    completed = session.navigate_console_step("next")

    assert completed["kitchen_state"] == "COMPLETED"
    assert completed["session_active"] is False
    assert completed["robot_action"] == "high_five"
    assert "完成" in completed["display"]
    assert session.status_snapshot()["state"] == "COMPLETED"


def test_console_status_exposes_parallel_prep_during_timer() -> None:
    session = KitchenSession(clock=lambda: 100.0)
    session.current_recipe = session.normalizer.normalize({
        "name": "并行准备测试菜",
        "ingredients": [
            {"name": "鸡肉", "amount": 300, "unit": "克"},
            {"name": "盐", "amount": 2, "unit": "克"},
            {"name": "洋葱", "amount": 100, "unit": "克"},
        ],
        "steps": [
            {"instruction": "备好食材。"},
            {"instruction": "鸡肉加入 2 克盐抓匀后腌制 10 分钟。", "duration_seconds": 600},
            {"instruction": "将 100 克洋葱切丝。"},
            {"instruction": "鸡肉和洋葱下锅炒熟。", "duration_seconds": 300},
        ],
    })
    session.state = "COOKING"
    session.step_index = 0

    session.navigate_console_step("next")
    snapshot = session.status_snapshot()

    assert snapshot["parallel_step"] == {
        "number": 3,
        "instruction": "将 100 克洋葱切丝",
        "completed": False,
    }


def test_console_default_browse_count_is_not_capped_at_one_hundred() -> None:
    provider = MockRecipeSearchProvider(ROOT / "skills" / "kitchen_assistant" / "recipes")
    session = KitchenSession(recipe_provider=provider)

    rows = session.browse_local_recipes()

    assert len(rows) == 440


def test_console_categories_hide_uncategorized_generated_recipes(tmp_path: Path) -> None:
    generated = tmp_path / "generated"
    generated.mkdir()
    (generated / "cached_test.json").write_text(json.dumps({
        "cache_version": 5,
        "recipes": [{
            "recipe_id": "cached_test",
            "name": "测试缓存菜谱",
            "default_servings": 2,
            "ingredients": [{"name": "鸡肉", "amount": "300 克"}],
            "steps": [{"instruction": "将 300 克鸡肉煮熟。"}],
        }],
    }, ensure_ascii=False), encoding="utf-8")
    provider = MockRecipeSearchProvider(
        ROOT / "skills" / "kitchen_assistant" / "recipes",
        generated_dir=generated,
    )

    categories = provider.list_categories()
    rows = provider.list_recipes()

    assert all(item["id"] != "uncategorized" for item in categories)
    assert all(item["id"] for item in categories)
    assert len(rows) == sum(item["count"] for item in categories) == 440
    assert all(item["recipe_id"] != "cached_test" for item in rows)




def test_curry_chicken_rice_has_its_named_main_ingredients() -> None:
    provider = MockRecipeSearchProvider(ROOT / "skills" / "kitchen_assistant" / "recipes")

    recipe = provider.get_recipe_by_id("local_expansion_301")
    ingredients = {item["name"] for item in recipe["ingredients"]}

    assert {"鸡腿肉", "咖喱块", "米饭"} <= ingredients
    assert len(recipe["steps"]) >= 6


def test_lan_console_serves_page_status_and_rejects_cross_origin_post() -> None:
    vision = IngredientVisionService(FakeCamera(), PantryVisionClient())
    service = KitchenConsoleService(FakeManager(), FakeExecutor(), vision)
    server = KitchenConsoleServer(service, host="127.0.0.1", port=0)
    server.start()
    base = f"http://127.0.0.1:{server.port}"
    try:
        with urlopen(f"{base}/", timeout=2) as response:
            page = response.read().decode("utf-8")
        assert "厨房助手控制台" in page

        with urlopen(f"{base}/api/status", timeout=2) as response:
            status = json.loads(response.read())
        assert status["robot"]["state"] == "idle"
        assert "不提供" in status["scope_notice"]

        with urlopen(f"{base}/api/recipes?q=%E7%95%AA%E8%8C%84", timeout=2) as response:
            recipes = json.loads(response.read())
        assert recipes["recipes"][0]["name"] == "番茄炒蛋"

        request = Request(
            f"{base}/api/camera/capture",
            method="POST",
            headers={"Origin": "http://attacker.invalid"},
        )
        try:
            urlopen(request, timeout=2)
            assert False, "cross-origin POST should fail"
        except Exception as exc:
            assert getattr(exc, "code", None) == 403
    finally:
        server.stop()
