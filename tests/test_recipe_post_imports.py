import base64
import json
import struct
from copy import deepcopy
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from runtime_core import video_imports, recipe_post_media
from runtime_core.video_imports import VideoImportService, VideoSource, QwenVideoModel, parse_xiaohongshu_html, VideoImportError
from runtime_core.video_media import VideoMediaError


def post_page(rows=None, desc="做法正文"):
    return json.dumps({"noteData": {"title": "咖喱沙茶牛排骨", "noteId": "abcdef123456", "type": "normal",
                                   "desc": desc, "imageList": rows or []}})


def test_parser_preserves_order_prefers_full_images_and_body():
    full = "https://8.8.8.8/first.jpg"
    source = parse_xiaohongshu_html(post_page([
        {"url": "https://8.8.8.8/thumb.jpg", "infoList": [
            {"url": "https://8.8.8.8/preview.jpg", "imageScene": "CRD_PRV"},
            {"url": full, "imageScene": "WB_DFT"}]},
        {"url": "https://8.8.8.8/second.jpg"}, {"url": full}],
        desc="正文" * 2000), source_url="https://xhslink.cn/o/example")
    assert source.content_kind == "image_post"
    assert source.image_urls == [full, "https://8.8.8.8/second.jpg"]
    assert source.image_count == 2
    assert len(source.description) == 4000
    assert "image_urls" not in source.safe_metadata()


@pytest.mark.parametrize("rows", [
    [{"url": "http://127.0.0.1/image.jpg"}], [{"url": "file:///tmp/img.jpg"}],
    [{"url": ""}], [{"url": "https://8.8.8.8/img.jpg"}] * 17,
])
def test_post_invalid_images_are_rejected(rows):
    with pytest.raises(VideoImportError):
        parse_xiaohongshu_html(post_page(rows))


@pytest.mark.parametrize("ext", [".jpg", ".png", ".webp"])
def test_preparation_resizes_supported_formats(ext):
    original = np.zeros((2000, 1000, 3), dtype=np.uint8)
    ok, data = cv2.imencode(ext, original)
    assert ok
    assert recipe_post_media.image_dimensions(data.tobytes()) == (1000, 2000)
    jpeg = recipe_post_media.prepare_image(data.tobytes())
    assert recipe_post_media.image_dimensions(jpeg) == (800, 1600)


def test_oversized_header_rejected_before_decode(monkeypatch):
    def decode(*args):
        pytest.fail("oversized image must be rejected before decode")
    monkeypatch.setattr(cv2, "imdecode", decode)
    data = b"\x89PNG\r\n\x1a\n" + b"\0" * 8 + struct.pack(">II", 10000, 10000)
    with pytest.raises(VideoMediaError) as failure:
        recipe_post_media.prepare_image(data)
    assert failure.value.status_code == 413


def test_download_keeps_order_and_bounded_fetch(monkeypatch):
    ok, data = cv2.imencode(".jpg", np.zeros((2, 3, 3), dtype=np.uint8))
    calls = []
    def fetch(url, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(data=data.tobytes())
    monkeypatch.setattr(recipe_post_media, "fetch_public_resource", fetch)
    urls, size = recipe_post_media.download_post_images(["https://8.8.8.8/a.jpg", "https://8.8.8.8/b.jpg"])
    assert len(urls) == 2
    assert all(url.startswith("data:image/jpeg;base64,") for url in urls)
    assert size == sum(len(base64.b64decode(url.split(",")[1])) for url in urls)
    assert all(call["max_bytes"] == recipe_post_media.MAX_IMAGE_BYTES and call["timeout"] == 15 for call in calls)


def test_download_private_url_rejected():
    with pytest.raises(VideoMediaError):
        recipe_post_media.download_post_images(["http://127.0.0.1/image.jpg"])


def recipe_result():
    return {
        "name": "咖喱沙茶牛排骨", "estimated_time_minutes": 50, "difficulty": "简单",
        "ingredients": [{"name": "牛排骨", "amount": 250, "unit": "克", "optional": False},
                        {"name": "沙茶酱", "amount": "足量", "unit": "", "optional": False}],
        "steps": [{"instruction": "牛排骨切块洗净。", "duration_seconds": None, "heat_level": None},
                  {"instruction": "加入沙茶酱和清水，小火炖煮40分钟至软烂。", "duration_seconds": 2400, "heat_level": "小火"}],
        "source_text": [{"image_index": 2, "text": "小火40分钟"}],
        "evidence": [{"field": "steps[1].duration_seconds", "value": "40分钟", "image_index": 2, "quote": "小火40分钟", "confidence": "高"},
                     {"field": "ingredients[0].amount", "value": 250, "image_index": 99}],
        "ai_completion_fields": ["ingredients[0].amount", "ingredients[0].unit"],
        "is_tutorial": True, "dish_count": 1,
    }


@pytest.mark.parametrize("image_count", [0, 2])
def test_post_full_pipeline_preview_exact_confirmation_and_cleanup(tmp_path, monkeypatch, image_count):
    source = VideoSource(content_kind="image_post", image_urls=["https://8.8.8.8/a.jpg?secret=token"] * image_count,
                         image_count=image_count, title="咖喱沙茶牛排骨", description="小火40分钟", platform="xiaohongshu",
                         original_url="https://xhslink.cn/o/example")
    data = [f"data:image/jpeg;base64,{i}" for i in range(image_count)]
    monkeypatch.setattr(video_imports, "download_post_images", lambda urls: (list(data), 200))
    def unexpected(*args):
        pytest.fail("posts must skip video decoding")
    calls = []
    class Model:
        last_usage = {"input_tokens": 100, "output_tokens": 200}
        def analyze_images(self, urls, prompt, *, partial_callback):
            calls.append((list(urls), prompt))
            partial_callback({"ingredients": recipe_result()["ingredients"], "steps": recipe_result()["steps"]})
            return recipe_result()
        def organize_text(self, text, prompt, *, partial_callback):
            calls.append((text, prompt))
            partial_callback({"ingredients": recipe_result()["ingredients"]})
            return recipe_result()
    service = VideoImportService(adapter=SimpleNamespace(resolve=lambda share: source), model=Model(),
                                 store_dir=tmp_path/"recipes", temp_dir=tmp_path/"media", media_validator=unexpected)
    state = service.submit_link("https://xhslink.cn/o/example")
    state = service.wait(state["id"], timeout=10)
    assert state["stage"] == "review", state.get("error")
    assert state["draft"]["servings"] == 1
    assert len(calls) == 1
    if image_count:
        assert calls[0][0] == data
    assert state["metrics"]["model_requests"] == 1
    assert state["metrics"]["compression_seconds"] == 0
    assert state["metrics"]["first_preview_seconds"] is not None
    assert not state["draft"]["import_metadata"]["completion_needed"]
    draft = deepcopy(state["draft"])
    metadata = draft["import_metadata"]
    assert metadata["source"]["content_kind"] == "image_post"
    assert metadata["source"]["image_count"] == image_count
    assert "duration_seconds" not in metadata["source"]
    assert "secret=token" not in json.dumps(state)
    assert source.image_data_urls == source.image_urls == []
    assert service._tasks[state["id"]].temp_dir is None
    if image_count:
        assert metadata["field_origins"]["steps[1].duration_seconds"]["origin"] == "post_fact"
    saved = service.confirm(state["id"])["recipe"]
    assert saved["source_name"] == "小红书图文"
    for field in ("name", "servings", "ingredients", "steps"):
        assert saved[field] == draft[field]


def test_vision_request_single_ordered_multimodal_stream_without_omni_flags():
    calls = []
    def create(**kwargs):
        calls.append(kwargs)
        return iter([{"choices": [{"delta": {"content": json.dumps(recipe_result(), ensure_ascii=False)}}]}])
    model = QwenVideoModel(client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))),
                           config=SimpleNamespace(vision_model="qwen3-vl-flash", api_key="test"))
    urls = ["data:image/jpeg;base64,AAAA", "data:image/jpeg;base64,BBBB"]
    model.analyze_images(urls, "正文")
    assert len(calls) == 1
    assert calls[0]["model"] == "qwen3-vl-flash"
    assert calls[0]["response_format"] == {"type": "json_object"}
    assert "modalities" not in calls[0]
    assert calls[0]["stream"] is True
    assert [item["image_url"]["url"] for item in calls[0]["messages"][0]["content"][1:]] == urls


def test_post_conflicting_ai_provenance_is_not_claimed_as_fact(tmp_path, monkeypatch):
    source = VideoSource(content_kind="image_post", image_urls=["https://8.8.8.8/image.jpg"], image_count=1,
                         title="牛排骨", platform="xiaohongshu")
    monkeypatch.setattr(video_imports, "download_post_images", lambda urls: (["data:image/jpeg;base64,AAAA"], 1))
    def analyze(*args, **kwargs):
        result = recipe_result()
        result["evidence"].append({"field": "ingredients.0.amount", "value": 250, "image_index": 1})
        return result
    service = VideoImportService(adapter=SimpleNamespace(resolve=lambda share: source),
                                 model=SimpleNamespace(analyze_images=analyze),
                                 store_dir=tmp_path/"recipes", temp_dir=tmp_path/"media")
    state = service.submit_link("https://xhslink.cn/o/example")
    state = service.wait(state["id"], timeout=5)
    assert state["stage"] == "review"
    assert state["draft"]["import_metadata"]["field_origins"]["ingredients[0].amount"]["origin"] == "ai_completion"


def test_split_preparation_keeps_image_evidence_and_ai_timer_on_correct_steps(tmp_path, monkeypatch):
    source = VideoSource(content_kind="image_post", image_urls=["https://8.8.8.8/a.jpg"], image_count=1, platform="xiaohongshu")
    monkeypatch.setattr(video_imports, "download_post_images", lambda urls: (["data:image/jpeg;base64,AAAA"], 1))
    def analyze(*args, **kwargs):
        return {
            "name": "牛排骨", "ingredients": [{"name": "牛排骨", "amount": 250, "unit": "克"}],
            "steps": [
                {"instruction": "牛排骨切块，生姜切片。", "duration_seconds": None},
                {"instruction": "牛排骨小火炖煮40分钟。", "duration_seconds": 2400},
                {"instruction": "最后大火收汁。", "duration_seconds": 300, "heat_level": "大火"},
            ],
            "evidence": [{"field": "steps[1].duration_seconds", "value": "40分钟", "image_index": 1, "quote": "小火炖煮40分钟"}],
            "source_text": [{"image_index": 1, "text": "小火炖煮40分钟"}],
        }
    service = VideoImportService(adapter=SimpleNamespace(resolve=lambda share: source),
                                 model=SimpleNamespace(analyze_images=analyze),
                                 store_dir=tmp_path/"recipes", temp_dir=tmp_path/"media")
    state = service.submit_link("https://xhslink.cn/o/example")
    state = service.wait(state["id"], timeout=5)
    assert state["stage"] == "review", state.get("error")
    assert state["metrics"]["model_requests"] == 1
    steps = state["draft"]["steps"]
    metadata = state["draft"]["import_metadata"]
    cook = next(i for i,s in enumerate(steps) if "炖煮" in s["instruction"])
    finish = next(i for i,s in enumerate(steps) if "收汁" in s["instruction"])
    assert metadata["field_origins"][f"steps[{cook}].duration_seconds"]["origin"] == "post_fact"
    assert metadata["field_origins"][f"steps[{finish}].duration_seconds"]["origin"] == "ai_completion"
    assert steps[finish]["duration_seconds"] == 300


def test_post_elapsed_context_does_not_start_second_countdown(tmp_path, monkeypatch):
    source = VideoSource(content_kind="image_post", image_urls=["https://8.8.8.8/a.jpg"], image_count=1, platform="xiaohongshu")
    monkeypatch.setattr(video_imports, "download_post_images", lambda urls: (["data:image/jpeg;base64,AAAA"], 1))
    def analyze(*args, **kwargs):
        result = recipe_result()
        result["steps"].append({"instruction": "炖煮30分钟后，挑出料渣，继续炖至软烂。", "duration_seconds": 1800})
        result["evidence"] = []
        return result
    service = VideoImportService(adapter=SimpleNamespace(resolve=lambda share: source),
                                 model=SimpleNamespace(analyze_images=analyze),
                                 store_dir=tmp_path/"recipes", temp_dir=tmp_path/"media")
    state = service.submit_link("https://xhslink.cn/o/example")
    state = service.wait(state["id"], timeout=5)
    assert state["stage"] == "review"
    elapsed = next(step for step in state["draft"]["steps"] if "挑出料渣" in step["instruction"])
    assert "不另计时" in elapsed["instruction"]
    assert elapsed["duration_seconds"] is None
    assert state["metrics"]["model_requests"] == 1


def test_post_evidence_requires_literal_quote_and_matching_seconds(tmp_path, monkeypatch):
    source = VideoSource(content_kind="image_post", image_urls=["https://8.8.8.8/a.jpg"], image_count=1, platform="xiaohongshu")
    monkeypatch.setattr(video_imports, "download_post_images", lambda urls: (["data:image/jpeg;base64,AAAA"], 1))
    def analyze(*args, **kwargs):
        result = recipe_result()
        result["source_text"] = [{"image_index": 1, "text": "小火40分钟"}]
        result["evidence"] = [
            {"field": "ingredients[0].amount", "value": 250, "image_index": 1, "quote": "牛肉250克"},
            {"field": "steps[1].duration_seconds", "value": 1800, "image_index": 1, "quote": "小火40分钟"},
        ]
        return result
    service = VideoImportService(adapter=SimpleNamespace(resolve=lambda share: source),
                                 model=SimpleNamespace(analyze_images=analyze),
                                 store_dir=tmp_path/"recipes", temp_dir=tmp_path/"media")
    state = service.submit_link("https://xhslink.cn/o/example")
    state = service.wait(state["id"], timeout=5)
    assert state["stage"] == "review"
    assert state["draft"]["import_metadata"]["evidence"] == []
    assert state["draft"]["steps"][-1]["duration_seconds"] == 2400


def test_post_wrong_field_index_cannot_override_another_steps_timer(tmp_path, monkeypatch):
    source = VideoSource(content_kind="image_post", image_urls=["https://8.8.8.8/a.jpg"], image_count=1, platform="xiaohongshu")
    monkeypatch.setattr(video_imports, "download_post_images", lambda urls: (["data:image/jpeg;base64,AAAA"], 1))
    def analyze(*args, **kwargs):
        result = recipe_result()
        result["source_text"] = [{"image_index": 1, "text": "小火40分钟"}]
        result["evidence"] = [{"field": "steps[0].duration_seconds", "value": 2400, "image_index": 1, "quote": "小火40分钟"}]
        result["steps"].append({"instruction": "炖煮中途添加热水，挑出料渣。", "duration_seconds": 1800})
        return result
    service = VideoImportService(adapter=SimpleNamespace(resolve=lambda share: source),
                                 model=SimpleNamespace(analyze_images=analyze),
                                 store_dir=tmp_path/"recipes", temp_dir=tmp_path/"media")
    state = service.submit_link("https://xhslink.cn/o/example")
    state = service.wait(state["id"], timeout=5)
    assert state["stage"] == "review"
    assert state["draft"]["steps"][0]["duration_seconds"] is None
    middle = next(step for step in state["draft"]["steps"] if "中途" in step["instruction"])
    assert middle["duration_seconds"] is None
    assert state["draft"]["import_metadata"]["evidence"] == []
