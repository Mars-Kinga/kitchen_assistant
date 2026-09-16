from __future__ import annotations

import json
import sys
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
import jsonschema

from runtime_core.kitchen_console import KitchenConsoleServer, KitchenConsoleService

SKILL_ROOT = Path(__file__).resolve().parents[1] / "skills" / "kitchen_assistant"
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

from kitchen.session_store import KitchenSession, KitchenSessionBusyError  # noqa: E402
from kitchen.recipe_normalizer import RecipeNormalizer  # noqa: E402


def reviewed_recipe(*, meat=False, first_timed=False):
    recipe = RecipeNormalizer().normalize({
        "recipe_id": "video_example", "name": "视频牛肉" if meat else "视频番茄",
        "servings": 2, "source_name": "小红书视频导入",
        "ingredients": [{"name": "牛肉" if meat else "番茄", "amount": 200, "unit": "克", "optional": False}],
        "steps": [
            {"instruction": "清水烧开，放入食材煮 2 分钟。" if first_timed else "将食材切块。", "duration_seconds": 120 if first_timed else None},
            {"instruction": "放入食材中火煮 2 分钟。", "duration_seconds": 120},
            {"instruction": "关火装盘。", "duration_seconds": None},
        ],
    })
    recipe["import_metadata"] = {"confirmed": True, "field_origins": {"servings": "ai_suggested"}}
    return recipe


def test_reviewed_workflow_is_not_normalized_again():
    class RejectNormalizer:
        def normalize(self, *args, **kwargs):
            raise AssertionError("Reviewed workflow must be used unchanged")

    session = KitchenSession(recipe_normalizer=RejectNormalizer())
    recipe = reviewed_recipe()
    result = session.select_imported_console_recipe(recipe)
    assert result["kitchen_state"] == "COOKING"
    assert session.current_recipe == recipe
    assert session.current_recipe is not recipe
    assert session.status_snapshot()["recipe"]["import_metadata"]["confirmed"]


def test_imported_selection_preserves_current_task_and_timer():
    session = KitchenSession(clock=lambda: 100.0)
    session.select_imported_console_recipe(reviewed_recipe(first_timed=True))
    session.console_cooking_action("start_timer")
    before = session.status_snapshot()
    with pytest.raises(KitchenSessionBusyError):
        session.select_imported_console_recipe(reviewed_recipe(meat=True))
    assert session.status_snapshot() == before


def test_imported_timed_step_requires_explicit_start_and_confirm():
    session = KitchenSession(clock=lambda: 100.0)
    session.select_imported_console_recipe(reviewed_recipe(first_timed=True))
    assert session.timer is None
    session.navigate_console_step("next")
    assert session.step_index == 0
    assert session.pending_unstarted_timer_confirmation
    session.console_cooking_action("start_timer")
    assert session.timer.seconds == 120
    session.navigate_console_step("next")
    assert session.step_index == 0
    assert session.pending_timer_skip_confirmation
    session.console_cooking_action("confirm_done")
    assert session.step_index == 1
    assert session.timer is None


def test_imported_meat_precondition_and_console_pause_resume():
    now = [100.0]
    session = KitchenSession(clock=lambda: now[0])
    result = session.select_imported_console_recipe(reviewed_recipe(meat=True, first_timed=True))
    assert result["kitchen_state"] == "WAITING_MEAT_THAW"
    with pytest.raises(ValueError):
        session.navigate_console_step("next")
    session.console_cooking_action("fresh_ingredients")
    assert session.state == "COOKING" and session.timer is None
    session.console_cooking_action("start_timer")
    now[0] = 110.0
    session.console_cooking_action("pause")
    assert session.timer.paused_remaining_seconds == 110
    now[0] = 150.0
    session.console_cooking_action("resume")
    assert session.timer.deadline == 260.0
    session.console_cooking_action("cancel_task")
    assert session.state == "CANCELLED" and session.timer is None


def test_imported_recipe_finishes_through_valid_robot_feedback_without_auto_advancing():
    now = [100.0]
    session = KitchenSession(clock=lambda: now[0])
    recipe = reviewed_recipe(meat=True)
    recipe["steps"][1]["duration_seconds"] = 1500
    recipe["steps"][1]["instruction"] = "按锅具说明盖盖，上汽后小火压 25 分钟。"
    schema = json.loads((SKILL_ROOT / "schemas" / "output_schema.json").read_text())
    outputs = [session.select_imported_console_recipe(recipe)]
    outputs.append(session.console_cooking_action("fresh_ingredients"))
    outputs.append(session.navigate_console_step("next"))
    assert session.step_index == 1 and session.timer is None
    outputs.append(session.console_cooking_action("start_timer"))
    assert session.timer.seconds == 1500
    now[0] += 1501
    outputs.append(session.poll())
    assert session.step_index == 1 and session.state == "COOKING"
    outputs.append(session.navigate_console_step("next"))
    outputs.append(session.navigate_console_step("next"))
    assert session.state == "COMPLETED"
    for output in outputs:
        jsonschema.validate(output, schema)


class FakeVideos:
    def __init__(self):
        self.recipe = reviewed_recipe()
        self.calls = []

    def submit_link(self, share_text):
        self.calls.append(("link", share_text))
        return {"id": "task1", "stage": "review", "draft": self.recipe}

    def submit_upload(self, filename, data):
        self.calls.append(("upload", filename, data))
        return {"id": "task1", "stage": "review", "draft": self.recipe}

    def get(self, task_id):
        if task_id != "task1":
            raise KeyError(task_id)
        return {"id": task_id, "stage": "review", "draft": self.recipe}

    def update_draft(self, task_id, payload):
        self.calls.append(("edit", task_id, payload))
        return {"id": task_id, "stage": "review", "draft": {**self.recipe, **payload}}

    def confirm(self, task_id):
        return {"recipe_id": self.recipe["recipe_id"], "recipe": self.recipe}

    def complete_draft(self, task_id):
        self.calls.append(("complete", task_id))
        return {"id": task_id, "stage": "review", "draft": self.recipe}

    def cancel(self, task_id):
        return {"id": task_id, "stage": "cancelled"}

    def list_recipes(self):
        return [self.recipe]

    def recipe_detail(self, recipe_id, *, servings=2):
        self.calls.append(("detail", recipe_id, servings))
        return deepcopy(self.recipe)


@pytest.fixture
def http_console():
    session = KitchenSession()
    videos = FakeVideos()

    class Manager:
        def call_skill_hook(self, skill, hook, *args, **kwargs):
            kwargs.pop("validate_output", None)
            kwargs.pop("activate_session", None)
            return getattr(session, hook)(*args, **kwargs)

    service = KitchenConsoleService(
        Manager(), SimpleNamespace(execute_plan=lambda plan: None, status_snapshot=lambda: {}),
        SimpleNamespace(status_snapshot=lambda: {}), video_service=videos,
    )
    server = KitchenConsoleServer(service, host="127.0.0.1", port=0)
    server.start()
    yield f"http://127.0.0.1:{server.port}", videos, session
    server.stop()


def request(base, path, *, method="GET", body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    with urlopen(Request(base + path, data=data, method=method, headers=headers or {"Content-Type": "application/json"}), timeout=5) as response:
        return response.status, json.load(response)


def test_video_http_draft_confirmation_selection_and_conflict(http_console):
    base, videos, session = http_console
    assert request(base, "/api/video-imports", method="POST", body={"share_text": "链接 https://xhslink.cn/o/example"})[0] == 202
    assert request(base, "/api/video-imports/task1")[1]["stage"] == "review"
    request(base, "/api/video-imports/task1/draft", method="PATCH", body={"name": "修改菜名"})
    assert videos.calls[-1][0] == "edit"
    assert request(base, "/api/video-imports/task1/complete", method="POST", body={})[1]["stage"] == "review"
    assert videos.calls[-1] == ("complete", "task1")
    assert session.state == "IDLE"
    assert request(base, "/api/video-imports/task1/confirm", method="POST")[1]["recipe_id"] == "video_example"
    assert session.state == "IDLE"
    assert request(base, "/api/imported-recipes")[1]["count"] == 1
    assert request(base, "/api/recipes/video_example")[1]["import_metadata"]["confirmed"]
    assert request(base, "/api/recipes/select", method="POST", body={"recipe_id": "video_example"})[1]["kitchen_state"] == "COOKING"
    with pytest.raises(HTTPError) as conflict:
        request(base, "/api/recipes/select", method="POST", body={"recipe_id": "video_example"})
    assert conflict.value.code == 409
    assert request(base, "/api/cooking/action", method="POST", body={"action": "cancel_task"})[1]["status"]["state"] == "CANCELLED"
    assert request(base, "/api/video-imports/task1", method="DELETE")[1]["stage"] == "cancelled"


def test_video_http_upload_and_cross_origin_mutations(http_console):
    base, videos, _ = http_console
    boundary = "runtime-video-boundary"
    data = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="demo.mp4"\r\nContent-Type: video/mp4\r\n\r\n'.encode() + b"video bytes" + f"\r\n--{boundary}--\r\n".encode())
    with urlopen(Request(base + "/api/video-imports", data=data, headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}), timeout=5) as response:
        assert response.status == 202
    assert videos.calls[-1] == ("upload", "demo.mp4", b"video bytes")
    for method, path in [("POST", "/api/video-imports"), ("POST", "/api/video-imports/task1/complete"), ("PATCH", "/api/video-imports/task1/draft"), ("DELETE", "/api/video-imports/task1")]:
        with pytest.raises(HTTPError) as forbidden:
            request(base, path, method=method, body={}, headers={"Origin": "https://untrusted.example", "Content-Type": "application/json"})
        assert forbidden.value.code == 403


def test_video_details_default_to_one_person_and_preserve_explicit_size(http_console):
    base, videos, _ = http_console
    request(base, "/api/recipes/video_example")
    assert videos.calls[-1] == ("detail", "video_example", 1)
    request(base, "/api/recipes/video_example?servings=3")
    assert videos.calls[-1] == ("detail", "video_example", 3)
