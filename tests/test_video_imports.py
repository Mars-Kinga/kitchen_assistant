from __future__ import annotations

import time
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import threading

import pytest

from runtime_core.video_imports import (
    PublicXiaohongshuAdapter,
    QwenVideoModel,
    VideoImportError,
    VideoImportService,
    VideoSource,
    _ImportTask,
    parse_xiaohongshu_html,
    _merge_completion_draft,
    _completion_fields_for_draft,
    _validate_recipe_quality,
)
from runtime_core.video_media import MediaInfo, model_payload_size


class _Adapter:
    def resolve(self, _share_text: str) -> VideoSource:
        return VideoSource(
            video_bytes=b"tiny video fixture",
            title="视频牛肉",
            description="单道菜教程",
            source_url="https://www.xiaohongshu.com/explore/abc123456789",
            original_url="https://xhslink.cn/o/demo",
            note_id="abc123456789",
            platform="xiaohongshu",
        )


class _Model:
    def is_available(self) -> bool:
        return True

    def analyze_video(self, _data_url: str, _prompt: str) -> dict:
        return {
            "name": "视频牛肉",
            "servings": 2,
            "estimated_time_minutes": 30,
            "difficulty": "简单",
            "ingredients": [{"name": "牛肉", "amount": 200, "unit": "克", "optional": False}],
            "steps": [{"instruction": "加入200克牛肉，小火炖煮。", "duration_seconds": 120, "heat_level": "小火", "safety_note": None}],
            "evidence": [
                {"field": "ingredients[0].amount", "value": 200, "start_seconds": 10, "end_seconds": 12, "confidence": "高"},
                {"field": "steps[0].duration_seconds", "value": 120, "start_seconds": 10, "end_seconds": 12, "confidence": "高"},
            ],
        }


def _validator(path: Path) -> MediaInfo:
    return MediaInfo(path, ".mp4", "mp4", 65.0, path.stat().st_size, "video/mp4")


def test_ai_amount_completion_updates_unit_and_preserves_source_prose():
    draft = {"name": "三杯鸡", "ingredients": [
        {"name": "洋葱丝", "amount": "", "unit": "丝"},
        {"name": "米酒", "amount": "一圈", "unit": "圈"},
        {"name": "鸡肉", "amount": 600, "unit": "克"},
    ], "steps": [{"instruction": "淋一圈米酒，焖2分钟。", "duration_seconds": 120}]}
    result, fields = _merge_completion_draft(draft, {"ingredients": [
        {"name": "米酒", "amount": 30, "unit": "ml"},
        {"name": "洋葱丝", "amount": 100, "unit": "克"},
    ]})
    assert result["ingredients"][0]["amount"] == 100
    assert result["ingredients"][0]["unit"] == "克"
    assert result["ingredients"][1]["amount"] == "一圈"
    assert result["ingredients"][1]["unit"] == "圈"
    assert result["ingredients"][2]["amount"] == 600
    assert result["ingredients"][2]["unit"] == "克"
    assert result["steps"][0]["instruction"] == draft["steps"][0]["instruction"]
    assert result["steps"][0]["duration_seconds"] == draft["steps"][0]["duration_seconds"]
    assert "ingredients[0].unit" in fields
    assert _completion_fields_for_draft(result) == []


def test_main_meat_prefers_ai_quantity_but_condiments_keep_qualitative_amount():
    draft = {"name": "鸡腿饭", "servings": 1, "ingredients": [
        {"name": "鸡腿肉", "amount": "适量", "unit": "块"},
        {"name": "盐", "amount": "适量", "unit": None},
        {"name": "牛肉", "amount": 200, "unit": "克"},
    ], "steps": [{"instruction": "把肉和调料拌匀。", "duration_seconds": None}]}
    assert _completion_fields_for_draft(draft) == ["ingredients[0].amount", "ingredients[0].unit"]
    result, fields = _merge_completion_draft(draft, {"ingredients": [
        {"name": "鸡腿肉", "amount": 250, "unit": "克"},
        {"name": "盐", "amount": 2, "unit": "克"},
        {"name": "牛肉", "amount": 500, "unit": "克"},
    ]})
    assert result["ingredients"][0]["amount"] == 250
    assert result["ingredients"][0]["unit"] == "克"
    assert result["ingredients"][1]["amount"] == "适量"
    assert result["ingredients"][2]["amount"] == 200
    assert "ingredients[0].amount" in fields
    assert _completion_fields_for_draft(result) == []


@pytest.mark.parametrize("amount", ["适量", "少量", "按口味", "一圈"])
def test_qualitative_amount_is_confirmable_without_numeric_completion(amount, tmp_path):
    draft = {"name": "三杯鸡", "servings": 2, "ingredients": [{"name": "盐", "amount": amount, "unit": None}],
             "steps": [{"instruction": "放入调味料拌匀。", "duration_seconds": None}]}
    assert _completion_fields_for_draft(draft) == []
    _validate_recipe_quality(draft, {"quality_issues": ["视频未明确数字用量"]})
    VideoImportService._validate_review_runtime_shape(draft)
    service = VideoImportService(store_dir=tmp_path / "recipes", model=_Model())
    assert service._normalize_for_runtime(draft)["ingredients"][0]["amount"] == amount


def _wait(service: VideoImportService, task_id: str) -> dict:
    for _ in range(200):
        snapshot = service.get(task_id)
        if snapshot["stage"] in {"review", "failed", "cancelled"}:
            return snapshot
        time.sleep(0.005)
    return service.get(task_id)


def _seed_review_task(
    service: VideoImportService,
    tmp_path: Path,
    draft: dict,
    *,
    task_id: str = "review-task",
) -> _ImportTask:
    source_path = tmp_path / f"{task_id}.mp4"
    source_path.write_bytes(b"fixture")
    source = VideoSource(
        title=str(draft.get("name") or "视频菜"),
        platform="upload",
        original_url=f"upload://{task_id}.mp4",
    )
    task = _ImportTask(task_id=task_id, source_kind="upload", source_key=f"upload:{task_id}")
    task.source = source
    task.media_info = MediaInfo(source_path, ".mp4", "mp4", 65.0, source_path.stat().st_size, "video/mp4")
    task.stage = "review"
    task.draft = deepcopy(draft)
    task.draft_revision = 1
    task.finished_at = time.monotonic()
    service._tasks[task_id] = task
    service._active_task_id = task_id
    return task


def _review_draft(*, amount: object = 200, duration: object = 120, quality_issues: list[str] | None = None) -> dict:
    return {
        "name": "视频菜",
        "servings": 2,
        "estimated_time_minutes": 30,
        "difficulty": "简单",
        "ingredients": [{"name": "鸡肉", "amount": amount, "unit": "克", "optional": False}],
        "equipment": [],
        "notes": [],
        "steps": [{"instruction": "小火炖煮", "duration_seconds": duration, "heat_level": "小火", "safety_note": None}],
        "import_metadata": {
            "confirmed": False,
            "source": {"platform": "upload", "title": "视频菜", "source_url": "upload://review.mp4"},
            "evidence": [],
            "field_origins": {},
            "user_edits": [],
            "quality_issues": list(quality_issues or []),
            "missing_critical_fields": [],
        },
    }


def test_import_review_confirm_and_serving_detail(tmp_path: Path) -> None:
    service = VideoImportService(
        store_dir=tmp_path / "recipes",
        temp_dir=tmp_path / "tmp",
        adapter=_Adapter(),
        model=_Model(),
        media_validator=_validator,
    )
    submitted = service.submit_link("教程 https://xhslink.cn/o/demo")
    reviewed = _wait(service, submitted["id"])
    assert reviewed["stage"] == "review"
    assert reviewed["draft"]["import_metadata"]["confirmed"] is False
    assert reviewed["draft"]["import_metadata"]["source"]["source_url"] == "https://xhslink.cn/o/demo"
    confirmed = service.confirm(submitted["id"])
    assert confirmed["recipe_id"].startswith("video_")
    assert confirmed["recipe"]["import_metadata"]["confirmed"] is True
    detail = service.recipe_detail(confirmed["recipe_id"], servings=4)
    assert detail["servings"] == 4
    assert detail["ingredients"][0]["amount"] == "400"
    assert "加入400克" in detail["steps"][0]["instruction"]
    assert detail["steps"][0]["duration_seconds"] == 120
    assert not list((tmp_path / "tmp").glob("video-import-*"))


def test_duplicate_submission_and_busy_slot(tmp_path: Path) -> None:
    class SlowModel(_Model):
        def analyze_video(self, data_url, prompt):
            time.sleep(0.04)
            return super().analyze_video(data_url, prompt)

    service = VideoImportService(
        store_dir=tmp_path / "recipes",
        temp_dir=tmp_path / "tmp",
        adapter=_Adapter(),
        model=SlowModel(),
        media_validator=_validator,
    )
    first = service.submit_link("https://xhslink.cn/o/demo")
    duplicate = service.submit_link("说明 https://xhslink.cn/o/demo?xsec_token=secret")
    assert duplicate["id"] == first["id"]
    with pytest.raises(VideoImportError) as error:
        service.submit_upload("other.mp4", b"another")
    assert error.value.status_code == 429
    _wait(service, first["id"])


def test_update_is_atomic_and_confirm_reuses_reviewed_version(tmp_path: Path) -> None:
    service = VideoImportService(
        store_dir=tmp_path / "recipes",
        temp_dir=tmp_path / "tmp",
        adapter=_Adapter(),
        model=_Model(),
        media_validator=_validator,
    )
    task = service.submit_upload("dish.mp4", b"fixture")
    reviewed = _wait(service, task["id"])
    before = reviewed["draft"]
    with pytest.raises(VideoImportError):
        service.update_draft(task["id"], {"servings": 2.5})
    assert service.get(task["id"])["draft"] == before
    updated = service.update_draft(task["id"], {"name": "修改后的牛肉"})
    assert updated["draft"]["name"] == "修改后的牛肉"
    confirmed = service.confirm(task["id"])
    assert confirmed["recipe"]["name"] == "修改后的牛肉"


def test_multi_field_update_is_atomic_when_a_later_field_is_invalid(tmp_path: Path) -> None:
    service = VideoImportService(
        store_dir=tmp_path / "recipes",
        temp_dir=tmp_path / "tmp",
        adapter=_Adapter(),
        model=_Model(),
        media_validator=_validator,
    )
    task = service.submit_upload("dish.mp4", b"fixture")
    reviewed = _wait(service, task["id"])
    before = deepcopy(reviewed["draft"])
    with pytest.raises(VideoImportError):
        service.update_draft(task["id"], {"name": "不应保存", "servings": 2.5})
    assert service.get(task["id"])["draft"] == before


def test_confirm_persists_the_review_snapshot_without_a_second_normalization(tmp_path: Path) -> None:
    from runtime_core.video_imports import _normalizer

    class CountingNormalizer:
        def __init__(self) -> None:
            self.calls = 0
            self.delegate = _normalizer()

        def normalize(self, raw, *, servings=None):
            self.calls += 1
            return self.delegate.normalize(raw, servings=servings)

    normalizer = CountingNormalizer()
    service = VideoImportService(
        store_dir=tmp_path / "recipes",
        temp_dir=tmp_path / "tmp",
        adapter=_Adapter(),
        model=_Model(),
        media_validator=_validator,
        normalizer=normalizer,
    )
    task = service.submit_upload("dish.mp4", b"fixture")
    reviewed = _wait(service, task["id"])
    assert reviewed["stage"] == "review"
    reviewed_steps = deepcopy(reviewed["draft"]["steps"])
    calls_before_confirm = normalizer.calls
    confirmed = service.confirm(task["id"])
    assert normalizer.calls == calls_before_confirm == 1
    assert confirmed["recipe"]["steps"] == reviewed_steps


def test_failed_and_cancelled_sources_can_be_retried(tmp_path: Path) -> None:
    class FailingModel(_Model):
        def analyze_video(self, data_url, prompt):
            raise VideoImportError("鉴权失败。", status_code=401)

    failed_service = VideoImportService(
        store_dir=tmp_path / "failed-recipes",
        temp_dir=tmp_path / "failed-tmp",
        adapter=_Adapter(),
        model=FailingModel(),
        media_validator=_validator,
    )
    first_failed = failed_service.submit_upload("dish.mp4", b"same")
    assert _wait(failed_service, first_failed["id"])["stage"] == "failed"
    retried_failed = failed_service.submit_upload("dish.mp4", b"same")
    assert retried_failed["id"] != first_failed["id"]

    release_model = threading.Event()

    class BlockingModel(_Model):
        def analyze_video(self, data_url, prompt):
            release_model.wait(1)
            return super().analyze_video(data_url, prompt)

    cancelled_service = VideoImportService(
        store_dir=tmp_path / "cancelled-recipes",
        temp_dir=tmp_path / "cancelled-tmp",
        adapter=_Adapter(),
        model=BlockingModel(),
        media_validator=_validator,
    )
    first_cancelled = cancelled_service.submit_upload("dish.mp4", b"same")
    for _ in range(100):
        if cancelled_service.get(first_cancelled["id"])["stage"] == "analyzing":
            break
        time.sleep(0.005)
    assert cancelled_service.cancel(first_cancelled["id"])["stage"] == "cancelled"
    release_model.set()
    assert _wait(cancelled_service, first_cancelled["id"])["stage"] == "cancelled"
    retried_cancelled = cancelled_service.submit_upload("dish.mp4", b"same")
    assert retried_cancelled["id"] != first_cancelled["id"]


def test_watchdog_fails_a_blocked_model_and_releases_the_single_slot(tmp_path: Path) -> None:
    release_model = threading.Event()

    class BlockingModel(_Model):
        def analyze_video(self, data_url, prompt):
            release_model.wait(3)
            return super().analyze_video(data_url, prompt)

    service = VideoImportService(
        store_dir=tmp_path / "recipes",
        temp_dir=tmp_path / "tmp",
        adapter=_Adapter(),
        model=BlockingModel(),
        media_validator=_validator,
        task_timeout_seconds=0.05,
    )
    first = service.submit_upload("dish.mp4", b"blocked")
    failed = _wait(service, first["id"])
    assert failed["stage"] == "failed"
    assert "超时" in (failed["error"] or "")
    release_model.set()
    service.wait(first["id"], timeout=3)
    retry = service.submit_upload("dish.mp4", b"blocked")
    assert retry["id"] != first["id"]
    assert _wait(service, retry["id"])["stage"] == "review"


def test_segment_transcode_bounds_portrait_video_and_preserves_segment_ranges(tmp_path: Path, monkeypatch) -> None:
    temp_dir = tmp_path / "video-import-segment"
    temp_dir.mkdir()
    source_path = temp_dir / "input.mp4"
    # A sparse file is enough to exercise the size branch without carrying a
    # multi-megabyte test fixture in the repository.
    with source_path.open("wb") as handle:
        handle.truncate(model_payload_size(0) + 1)
    task = _ImportTask(task_id="segment-test", source_kind="upload", source_key="segment")
    task.temp_dir = temp_dir
    task.media_info = MediaInfo(source_path, ".mp4", "mp4", 180.0, source_path.stat().st_size, "video/mp4")
    commands = []

    def fake_run(command, **kwargs):
        commands.append(list(command))
        Path(command[-1]).write_bytes(b"compressed")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("runtime_core.video_imports.shutil.which", lambda name: "/usr/bin/ffmpeg" if name == "ffmpeg" else None)
    monkeypatch.setattr("runtime_core.video_imports.subprocess.run", fake_run)
    segments = VideoImportService(temp_dir=tmp_path / "other-tmp", model=_Model())._make_segments(task)
    assert [(start, end) for _path, start, end in segments] == [(0.0, 120.0), (115.0, 180.0)]
    first = commands[0]
    assert first[first.index("-ss") + 1] == "0.000"
    assert first[first.index("-t") + 1] == "120.000"
    assert first[first.index("-vf") + 1].startswith("fps=5,scale=w='if(gt(iw,ih),720")
    assert first[first.index("-r") + 1] == "5"
    assert first[first.index("-threads") + 1] == "2"
    assert first[first.index("-map") + 1] == "0:v:0"
    assert "0:a:0?" in first


def test_qwen_video_model_uses_streaming_text_modality_and_reports_auth_errors() -> None:
    calls = []

    class Completions:
        def create(self, **kwargs):
            calls.append(kwargs)
            return iter([
                {"choices": [{"delta": {"content": '{"name":"视频菜"'}}]},
                {"choices": [{"delta": {"content": ',"ingredients":[],"steps":[]}'}}]},
            ])

    client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    model = QwenVideoModel(client=client, config=SimpleNamespace(api_key="test", base_url="https://example.invalid", max_retries=0))
    result = model.analyze_video("data:video/mp4;base64,AAAA", "提取")
    assert result["name"] == "视频菜"
    assert calls[0]["stream"] is True
    assert calls[0]["modalities"] == ["text"]
    assert calls[0]["extra_body"]["enable_thinking"] is False

    class AuthCompletions:
        def create(self, **kwargs):
            error = RuntimeError("unauthorized")
            error.status_code = 401
            error.body = {"error": {"code": "invalid_api_key"}}
            raise error

    auth_model = QwenVideoModel(
        client=SimpleNamespace(chat=SimpleNamespace(completions=AuthCompletions())),
        config=SimpleNamespace(api_key="test", base_url="https://example.invalid", max_retries=0),
    )
    with pytest.raises(VideoImportError) as error:
        auth_model.analyze_video("data:video/mp4;base64,AAAA", "提取")
    assert error.value.status_code == 401
    assert "鉴权" in str(error.value)
    assert "API Key 无效" in str(error.value)


def test_cancel_discards_late_worker_result(tmp_path: Path) -> None:
    class BlockingModel(_Model):
        def analyze_video(self, data_url, prompt):
            time.sleep(0.05)
            return super().analyze_video(data_url, prompt)

    service = VideoImportService(
        store_dir=tmp_path / "recipes",
        temp_dir=tmp_path / "tmp",
        adapter=_Adapter(),
        model=BlockingModel(),
        media_validator=_validator,
    )
    task = service.submit_upload("dish.mp4", b"fixture")
    service.cancel(task["id"])
    time.sleep(0.08)
    snapshot = service.get(task["id"])
    assert snapshot["stage"] == "cancelled"
    assert snapshot["draft"] is None
    assert service.list_recipes() == []


def test_public_html_parser_does_not_replace_undefined_inside_strings(monkeypatch) -> None:
    monkeypatch.setattr("runtime_core.video_imports.validate_public_url", lambda url: url)
    html = r'''<script>window.__INITIAL_STATE__={"noteData":{"video":{"media":{"video":{"duration":12},"stream":{"h264":[{"masterUrl":"https://8.8.8.8/video.mp4","duration":12000}]} }},"noteId":"note12345678","title":"contains undefined word","desc":"x"}}};</script>'''
    source = parse_xiaohongshu_html(html, source_url="https://xhslink.cn/o/demo")
    assert source.title == "contains undefined word"
    assert source.duration_seconds == 12.0
    assert source.note_id == "note12345678"
    assert source.original_url == "https://xhslink.cn/o/demo"


def test_streaming_preview_is_bounded_incomplete_and_collects_usage() -> None:
    callbacks = []

    class Usage:
        prompt_tokens = 123
        completion_tokens = 45

    class Completions:
        def create(self, **kwargs):
            return iter([
                {"choices": [{"delta": {"content": '{"name":"三杯鸡","ingredients":[{"name":"鸡腿","amount":300,"unit":"克"}],"steps":[{"instruction":"焯水","duration_seconds":600}'}}]},
                {"choices": [{"delta": {"content": ']}'}}]},
                {"choices": [], "usage": Usage()},
            ])

    model = QwenVideoModel(
        client=SimpleNamespace(chat=SimpleNamespace(completions=Completions())),
        config=SimpleNamespace(api_key="test", base_url="https://example.invalid", max_retries=0),
    )
    # The intentionally split JSON is completed by the second chunk; callback
    # previews must stay marked incomplete throughout collection.
    result = model.analyze_video(
        "data:video/mp4;base64,AAAA",
        "提取",
        partial_callback=callbacks.append,
    )
    assert result["name"] == "三杯鸡"
    assert callbacks
    assert all(item["complete"] is False for item in callbacks)
    assert callbacks[-1]["ingredients"][0]["name"] == "鸡腿"
    assert callbacks[-1]["steps"][0]["instruction"] == "焯水"
    assert model.last_usage == {"prompt_tokens": 123, "completion_tokens": 45}


def test_service_exposes_frozen_processing_metrics_and_partial_preview(tmp_path: Path) -> None:
    class PreviewModel(_Model):
        def analyze_video(self, data_url, prompt, *, partial_callback=None):
            if partial_callback is not None:
                partial_callback({
                    "name": "预览菜",
                    "ingredients": [{"name": "鸡肉", "amount": 200, "unit": "克"}],
                    "steps": [{"instruction": "先焯水", "duration_seconds": None, "heat_level": "小火"}],
                })
            return super().analyze_video(data_url, prompt)

    service = VideoImportService(
        store_dir=tmp_path / "recipes",
        temp_dir=tmp_path / "tmp",
        adapter=_Adapter(),
        model=PreviewModel(),
        media_validator=_validator,
    )
    task = service.submit_upload("dish.mp4", b"fixture")
    reviewed = _wait(service, task["id"])
    assert reviewed["stage"] == "review"
    assert reviewed["partial_preview"]["complete"] is False
    assert reviewed["partial_preview"]["steps"][0]["instruction"] == "先焯水"
    metrics = reviewed["metrics"]
    assert set(("fetching", "analyzing", "structuring")) <= set(metrics["stage_seconds"])
    assert metrics["model_requests"] == 1
    assert metrics["first_preview_seconds"] is not None
    frozen_elapsed = metrics["elapsed_seconds"]
    time.sleep(0.02)
    assert service.get(task["id"])["metrics"]["elapsed_seconds"] == frozen_elapsed


def test_short_single_segment_uses_one_complete_model_request(tmp_path: Path) -> None:
    class CountingModel(_Model):
        def __init__(self):
            self.calls = 0
            self.prompt = ""

        def analyze_video(self, data_url, prompt):
            self.calls += 1
            self.prompt = prompt
            return super().analyze_video(data_url, prompt)

    model = CountingModel()
    service = VideoImportService(
        store_dir=tmp_path / "recipes",
        temp_dir=tmp_path / "tmp",
        adapter=_Adapter(),
        model=model,
        media_validator=_validator,
    )
    task = service.submit_upload("dish.mp4", b"fixture")
    assert _wait(service, task["id"])["stage"] == "review"
    assert model.calls == 1
    assert '"name"' in model.prompt


def test_multi_segment_organizer_receives_text_evidence_once(tmp_path: Path) -> None:
    class MultiModel:
        def __init__(self) -> None:
            self.video_calls = 0
            self.text_calls = 0
            self.text_payload = ""

        def is_available(self) -> bool:
            return True

        def analyze_video(self, data_url, prompt, *, partial_callback=None):
            self.video_calls += 1
            if self.video_calls == 1:
                ingredient = {"name": "鸡肉", "amount": 300, "unit": "克", "optional": False}
            else:
                ingredient = {"name": "酱油", "amount": 15, "unit": "毫升", "optional": False}
            return {
                "title": "分段菜",
                "ingredients": [ingredient],
                "steps": [{"instruction": f"处理{ingredient['name']}", "duration_seconds": None}],
                "evidence": [{"field": "ingredients[0].name", "value": ingredient["name"], "start_seconds": 5, "end_seconds": 6, "confidence": "高"}],
            }

        def organize_text(self, evidence_text, prompt, *, partial_callback=None):
            self.text_calls += 1
            self.text_payload = evidence_text
            return {
                "name": "分段合并菜",
                "servings": 2,
                "ingredients": [
                    {"name": "鸡肉", "amount": 300, "unit": "克", "optional": False},
                    {"name": "酱油", "amount": 15, "unit": "毫升", "optional": False},
                ],
                "steps": [{"instruction": "合并处理后加热", "duration_seconds": 60}],
            }

    temp_dir = tmp_path / "video-import-multi"
    temp_dir.mkdir()
    first = temp_dir / "part0.mp4"
    second = temp_dir / "part1.mp4"
    first.write_bytes(b"part0")
    second.write_bytes(b"part1")
    task = _ImportTask(task_id="multi", source_kind="upload", source_key="multi")
    task.temp_dir = temp_dir
    task.media_info = MediaInfo(first, ".mp4", "mp4", 150.0, first.stat().st_size, "video/mp4")
    model = MultiModel()
    service = VideoImportService(store_dir=tmp_path / "recipes", temp_dir=tmp_path / "unused", model=model)
    service._make_segments = lambda _task: [(first, 0.0, 120.0), (second, 115.0, 150.0)]
    source = VideoSource(title="分段菜", platform="upload")
    _evidence, recipe = service._analyze_source(task, source)
    assert model.video_calls == 2
    assert model.text_calls == 1
    assert '"evidence"' in model.text_payload
    assert model.text_payload.count('"evidence"') == 1
    assert "data:video/" not in model.text_payload
    assert len(recipe["ingredients"]) == 2


def test_complete_draft_returns_review_snapshot_without_model_when_complete(tmp_path: Path) -> None:
    class NoCallModel:
        def __init__(self) -> None:
            self.calls = 0

        def complete_recipe(self, *args, **kwargs):
            self.calls += 1
            raise AssertionError("完整草稿不应发起 AI 补全")

    model = NoCallModel()
    service = VideoImportService(
        store_dir=tmp_path / "recipes",
        temp_dir=tmp_path / "tmp",
        model=model,
        media_validator=_validator,
    )
    task = _seed_review_task(service, tmp_path, _review_draft())

    snapshot = service.complete_draft(task.task_id)

    assert snapshot["stage"] == "review"
    assert snapshot["draft"]["ingredients"][0]["amount"] == 200
    assert model.calls == 0


def test_complete_draft_uses_text_only_completion_and_marks_ai_fields(tmp_path: Path) -> None:
    class CompletionModel:
        def __init__(self) -> None:
            self.calls = []

        def complete_recipe(self, recipe, missing_fields, quality_issues, *, title="", partial_callback=None):
            self.calls.append((deepcopy(recipe), list(missing_fields), list(quality_issues), title))
            return {
                "name": "视频菜",
                "servings": 2,
                "estimated_time_minutes": 30,
                "ingredients": [{"name": "鸡肉", "amount": 300, "unit": "克", "optional": False}],
                "steps": [{"instruction": "小火炖煮", "duration_seconds": 900, "heat_level": "小火"}],
                "ai_completion_fields": [
                    "ingredients[0].amount",
                    "steps[0].duration_seconds",
                ],
            }

    model = CompletionModel()
    service = VideoImportService(
        store_dir=tmp_path / "recipes",
        temp_dir=tmp_path / "tmp",
        model=model,
        media_validator=_validator,
    )
    task = _seed_review_task(
        service,
        tmp_path,
        _review_draft(amount="", duration=None, quality_issues=["来源未明确标注用量"]),
        task_id="complete-text",
    )

    snapshot = service.complete_draft(task.task_id)

    assert snapshot["stage"] == "review"
    assert len(model.calls) == 1
    recipe_payload, missing_fields, quality_issues, title = model.calls[0]
    assert "import_metadata" not in recipe_payload
    assert "evidence" not in recipe_payload
    assert "ingredients[0].amount" in missing_fields
    assert "steps[0].duration_seconds" in missing_fields
    assert quality_issues == ["来源未明确标注用量"]
    assert title == "视频菜"
    assert snapshot["draft"]["ingredients"][0]["amount"] == "300"
    assert snapshot["draft"]["name"] == "视频菜"
    assert snapshot["draft"]["servings"] == 2
    assert snapshot["draft"]["steps"][0]["instruction"] == "小火炖煮"
    assert snapshot["draft"]["steps"][0]["duration_seconds"] == 900
    metadata = snapshot["draft"]["import_metadata"]
    assert metadata["completion_needed"] is False
    assert metadata["ai_completed"] is True
    assert metadata["field_origins"]["ingredients[0].amount"]["origin"] == "ai_completion"
    assert metadata["field_origins"]["ingredients.0.amount"]["origin"] == "ai_completion"
    assert metadata["field_origins"]["steps[0].duration_seconds"]["evidence"] == []


def test_complete_draft_rejects_stale_result_after_concurrent_edit(tmp_path: Path) -> None:
    started = threading.Event()
    release = threading.Event()

    class BlockingCompletionModel:
        def complete_recipe(self, recipe, missing_fields, quality_issues, *, title="", partial_callback=None):
            started.set()
            assert release.wait(1.0)
            return {
                "ingredients": [{"name": "鸡肉", "amount": 300, "unit": "克"}],
                "steps": [{"instruction": "小火炖煮", "duration_seconds": 900}],
                "estimated_time_minutes": 30,
            }

    service = VideoImportService(
        store_dir=tmp_path / "recipes",
        temp_dir=tmp_path / "tmp",
        model=BlockingCompletionModel(),
        media_validator=_validator,
    )
    task = _seed_review_task(service, tmp_path, _review_draft(amount="", duration=None), task_id="stale-complete")
    outcome: list[object] = []

    def run_completion() -> None:
        try:
            outcome.append(service.complete_draft(task.task_id))
        except Exception as exc:  # noqa: BLE001 - assert the public status below
            outcome.append(exc)

    thread = threading.Thread(target=run_completion)
    thread.start()
    assert started.wait(1.0)
    service.update_draft(task.task_id, {"ingredients": [{"name": "鸡肉", "amount": 150, "unit": "克"}]})
    release.set()
    thread.join(2.0)

    assert len(outcome) == 1
    assert isinstance(outcome[0], VideoImportError)
    assert outcome[0].status_code == 409
    assert service.get(task.task_id)["draft"]["ingredients"][0]["amount"] == "150"


def test_quality_warning_is_notice_when_current_recipe_is_confirmable(tmp_path: Path) -> None:
    service = VideoImportService(
        store_dir=tmp_path / "recipes",
        temp_dir=tmp_path / "tmp",
        model=_Model(),
        media_validator=_validator,
    )
    task = _seed_review_task(
        service,
        tmp_path,
        _review_draft(quality_issues=["视频可能缺少一个装盘说明"]),
        task_id="quality-notice",
    )

    confirmed = service.confirm(task.task_id)

    assert confirmed["recipe_id"].startswith("video_")
    assert confirmed["recipe"]["import_metadata"]["confirmed"] is True
    assert confirmed["recipe"]["import_metadata"]["quality_issues"] == ["视频可能缺少一个装盘说明"]


def test_import_runs_one_text_completion_when_review_fields_are_missing(tmp_path: Path) -> None:
    class AutoCompletionModel:
        def __init__(self) -> None:
            self.video_calls = 0
            self.completion_calls = 0

        def is_available(self) -> bool:
            return True

        def analyze_video(self, data_url, prompt, *, partial_callback=None):
            self.video_calls += 1
            return {
                "name": "自动补全菜",
                "servings": 2,
                "ingredients": [{"name": "鸡肉", "amount": "", "unit": "克"}],
                "steps": [{"instruction": "切好鸡肉", "duration_seconds": None}],
                "quality_issues": ["视频未明确给出用量"],
            }

        def complete_recipe(self, recipe, missing_fields, quality_issues, *, title="", partial_callback=None):
            self.completion_calls += 1
            assert "ingredients[0].amount" in missing_fields
            assert "evidence" not in recipe
            return {
                "name": "自动补全菜",
                "estimated_time_minutes": 20,
                "ingredients": [{"name": "鸡肉", "amount": 250, "unit": "克"}],
                "steps": [{"instruction": "切好鸡肉", "duration_seconds": None}],
                "ai_completion_fields": ["ingredients[0].amount", "estimated_time_minutes"],
            }

    model = AutoCompletionModel()
    service = VideoImportService(
        store_dir=tmp_path / "recipes",
        temp_dir=tmp_path / "tmp",
        adapter=_Adapter(),
        model=model,
        media_validator=_validator,
    )
    submitted = service.submit_upload("dish.mp4", b"fixture")

    reviewed = _wait(service, submitted["id"])

    assert reviewed["stage"] == "review"
    assert model.video_calls == 1
    assert model.completion_calls == 1
    assert reviewed["draft"]["ingredients"][0]["amount"] == "250"
    assert reviewed["draft"]["import_metadata"]["ai_completed"] is True


def test_completion_failure_keeps_editable_review_draft(tmp_path: Path) -> None:
    class FailingCompletionModel:
        def complete_recipe(self, *args, **kwargs):
            raise VideoImportError("文本补全服务不可用。", status_code=503)

    service = VideoImportService(
        store_dir=tmp_path / "recipes",
        temp_dir=tmp_path / "tmp",
        model=FailingCompletionModel(),
        media_validator=_validator,
    )
    task = _seed_review_task(
        service,
        tmp_path,
        _review_draft(amount="", duration=None),
        task_id="completion-failure",
    )

    with pytest.raises(VideoImportError) as error:
        service.complete_draft(task.task_id)

    assert error.value.status_code == 503
    snapshot = service.get(task.task_id)
    assert snapshot["stage"] == "review"
    assert snapshot["draft"]["ingredients"][0]["amount"] == ""
    assert task.completion_in_progress is False


def test_review_fallback_corrects_explicit_timer_and_labels_unknown_fields(tmp_path: Path) -> None:
    """A normalization failure must not expose guessed timers as video facts."""

    source_path = tmp_path / "source.mp4"
    source_path.write_bytes(b"fixture")
    source = VideoSource(
        title="腌肉",
        description="单道菜教程",
        platform="upload",
        original_url="upload://source.mp4",
    )
    task = _ImportTask(task_id="fallback-timer", source_kind="upload", source_key="fallback-timer")
    task.source = source
    task.media_info = MediaInfo(source_path, ".mp4", "mp4", 65.0, source_path.stat().st_size, "video/mp4")
    service = VideoImportService(
        store_dir=tmp_path / "recipes",
        temp_dir=tmp_path / "tmp",
        adapter=_Adapter(),
        model=_Model(),
        media_validator=_validator,
    )
    # The blank amount intentionally makes runtime normalization fail, which
    # exercises the user-visible raw-draft fallback.
    raw = {
        "name": "腌肉",
        "servings": 2,
        "ingredients": [{"name": "猪肉", "amount": "", "unit": "克", "optional": False}],
        "steps": [
            {"instruction": "抓匀后腌制10分钟", "duration_seconds": 60},
            {"instruction": "大火煸炒至熟", "duration_seconds": 90},
            {"instruction": "煎至两面金黄", "duration_seconds": 120},
        ],
    }

    reviewed = service._prepare_review_draft(task, source, [], raw)

    assert reviewed["steps"][0]["duration_seconds"] == 600
    assert reviewed["steps"][1]["duration_seconds"] is None
    assert reviewed["steps"][2]["duration_seconds"] is None
    origins = reviewed["import_metadata"]["field_origins"]
    for path in (
        "name",
        "servings",
        "ingredients[0].name",
        "ingredients[0].amount",
        "steps[0].duration_seconds",
        "steps[1].duration_seconds",
        "steps[2].duration_seconds",
    ):
        assert origins[path]["origin"] == "ai_completion"
        assert origins[path]["evidence"] == []


def test_review_normalization_does_not_add_timer_to_untimed_marinade_or_prep(tmp_path: Path) -> None:
    """A user-fixed draft keeps untimed preparation steps untimed."""

    source_path = tmp_path / "source.mp4"
    source_path.write_bytes(b"fixture")
    source = VideoSource(title="腌肉", platform="upload", original_url="upload://source.mp4")
    task = _ImportTask(task_id="prep-timer", source_kind="upload", source_key="prep-timer")
    task.source = source
    task.media_info = MediaInfo(source_path, ".mp4", "mp4", 65.0, source_path.stat().st_size, "video/mp4")
    service = VideoImportService(
        store_dir=tmp_path / "recipes",
        temp_dir=tmp_path / "tmp",
        adapter=_Adapter(),
        model=_Model(),
        media_validator=_validator,
    )
    draft = {
        "name": "腌肉",
        "servings": 2,
        "ingredients": [{"name": "猪肉", "amount": 200, "unit": "克", "optional": False}],
        "steps": [
            {"instruction": "抓匀后腌制", "duration_seconds": None},
            {"instruction": "切葱", "duration_seconds": None},
        ],
        "import_metadata": {"confirmed": False, "evidence": [], "user_edits": []},
    }

    reviewed = service._standardize_review_draft(task, draft)

    assert reviewed["steps"][0]["duration_seconds"] is None
    assert "分钟" not in reviewed["steps"][0]["instruction"]
    assert reviewed["steps"][1]["duration_seconds"] is None
