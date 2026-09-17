import json
from types import SimpleNamespace

import pytest

from runtime_core import video_imports
from runtime_core.video_imports import CompositeXiaohongshuAdapter, PublicXiaohongshuAdapter, VideoImportError


def video_page(url="https://8.8.8.8/video.mp4"):
    return json.dumps({"noteData": {"title": "测试教程", "noteId": "test12345678", "video": {
        "media": {"video": {"duration": 57}, "stream": {"h264": [{"masterUrl": url}]}}
    }}}).encode()


def install_pages(monkeypatch, *pages):
    calls = []
    def fetch(url, **kwargs):
        calls.append((url, kwargs))
        return SimpleNamespace(data=pages[min(len(calls) - 1, len(pages) - 1)], final_url=url)
    monkeypatch.setattr(video_imports, "fetch_public_resource", fetch)
    return calls


def test_incomplete_share_page_is_retried_once_without_ai(monkeypatch):
    calls = install_pages(monkeypatch, b"<title>landing page</title>", video_page())
    source = PublicXiaohongshuAdapter().resolve("https://xhslink.cn/o/example")
    assert source.title == "测试教程"
    assert source.video_url == "https://8.8.8.8/video.mp4"
    assert source.original_url == "https://xhslink.cn/o/example"
    assert len(calls) == 2
    assert all("iPhone" in options["headers"]["User-Agent"] for _, options in calls)


def test_valid_share_page_does_not_retry(monkeypatch):
    calls = install_pages(monkeypatch, video_page())
    assert PublicXiaohongshuAdapter().resolve("https://xhslink.com/example").duration_seconds == 57
    assert len(calls) == 1


def test_incomplete_share_page_retry_is_bounded_and_reason_retained(monkeypatch):
    calls = install_pages(monkeypatch, b"<title>landing page</title>")
    adapter = CompositeXiaohongshuAdapter(public=PublicXiaohongshuAdapter(), wellbyte=SimpleNamespace(is_configured=False))
    with pytest.raises(VideoImportError) as failure:
        adapter.resolve("https://xhslink.cn/o/example")
    assert failure.value.reason == "incomplete_page"
    assert len(calls) == 2


def test_image_post_is_distinguished_and_does_not_call_video_fallback(monkeypatch):
    calls = install_pages(monkeypatch, json.dumps({"noteData": {"title": "图文菜谱", "type": "normal", "imageList": [{"url": "https://8.8.8.8/image.jpg"}]}}).encode())
    def unexpected_fallback(*args):
        pytest.fail("图文笔记不能转交视频下载通道")
    adapter = CompositeXiaohongshuAdapter(public=PublicXiaohongshuAdapter(), wellbyte=SimpleNamespace(is_configured=True, resolve=unexpected_fallback))
    source = adapter.resolve("https://xhslink.cn/o/example")
    assert source.content_kind == "image_post"
    assert source.image_urls == ["https://8.8.8.8/image.jpg"]
    assert source.image_count == 1
    assert len(calls) == 1


def test_private_video_url_is_not_retried_or_downloaded(monkeypatch):
    calls = install_pages(monkeypatch, video_page("http://127.0.0.1/video.mp4"))
    with pytest.raises(VideoImportError) as failure:
        PublicXiaohongshuAdapter().resolve("https://xhslink.cn/o/example")
    assert failure.value.status_code == 400
    assert len(calls) == 1
