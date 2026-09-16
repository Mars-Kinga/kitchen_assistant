from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace

import pytest

from runtime_core import video_media


class _Response:
    def __init__(self, body: bytes, headers: dict[str, str] | None = None) -> None:
        self.body = io.BytesIO(body)
        self.headers = headers or {}
        self.status = 200
        self.code = 200
        self.read_calls = 0
        self.closed = False

    def read(self, size: int = -1) -> bytes:
        self.read_calls += 1
        return self.body.read(size)

    def close(self) -> None:
        self.closed = True


class _Opener:
    def __init__(self, response: _Response) -> None:
        self.response = response

    def open(self, request, *, timeout: float):
        return self.response


def test_bounded_fetch_rejects_content_length_before_read(monkeypatch) -> None:
    monkeypatch.setattr(video_media, "_host_is_public", lambda _host: True)
    response = _Response(b"must not read", {"Content-Length": "65"})
    with pytest.raises(video_media.VideoMediaError) as error:
        video_media.fetch_public_resource("https://public.example/video.mp4", max_bytes=64, opener=_Opener(response))
    assert error.value.status_code == 413
    assert response.read_calls == 0
    assert response.closed


def test_public_url_blocks_private_literal_and_credentials() -> None:
    with pytest.raises(video_media.PublicURLBlocked):
        video_media.validate_public_url("http://127.0.0.1/video.mp4")
    with pytest.raises(video_media.PublicURLBlocked):
        video_media.validate_public_url("https://user:pass@8.8.8.8/video.mp4")


def test_probe_enforces_real_format_duration_and_video_stream(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "dish.mp4"
    path.write_bytes(b"fixture")

    def fake_run(*_args, **_kwargs):
        return SimpleNamespace(returncode=0, stdout="format_name=mp4\nduration=12\ncodec_type=video\n", stderr="")

    monkeypatch.setattr(video_media.subprocess, "run", fake_run)
    info = video_media.probe_video_file(path, ffprobe_path="ffprobe", ffmpeg_path="")
    assert info.format_name == "mp4"
    assert info.duration_seconds == 12

