"""Offline security and media regressions for video recipe imports.

These tests intentionally exercise the media boundary without making network
requests or requiring a local FFmpeg installation.  The import worker can use
the same fakes in its CI job while the live-link smoke test stays separate.
"""

from __future__ import annotations

import gzip
import io
import os
from pathlib import Path
from types import SimpleNamespace
from urllib.request import HTTPRedirectHandler

import pytest

from runtime_core import video_media


class _FakeResponse:
    def __init__(self, body: bytes = b"", *, status: int = 200, headers: dict[str, str] | None = None) -> None:
        self._stream = io.BytesIO(body)
        self.status = status
        self.code = status
        self.headers = headers or {}
        self.read_calls = 0
        self.closed = False

    def read(self, size: int = -1) -> bytes:
        self.read_calls += 1
        return self._stream.read(size)

    def close(self) -> None:
        self.closed = True


class _QueueOpener:
    def __init__(self, *responses: _FakeResponse) -> None:
        self.responses = list(responses)
        self.requests = []

    def open(self, request, *, timeout: float):
        self.requests.append((request, timeout))
        if not self.responses:
            raise AssertionError("the bounded fetch made an unexpected request")
        return self.responses.pop(0)


def test_validate_public_url_rejects_private_loopback_and_credentials() -> None:
    blocked_urls = (
        "http://127.0.0.1/video.mp4",
        "http://10.0.0.8/video.mp4",
        "http://192.168.1.20/video.mp4",
        "http://169.254.169.254/latest/meta-data",
        "http://[::1]/video.mp4",
        "https://user:password@8.8.8.8/video.mp4",
    )

    for url in blocked_urls:
        with pytest.raises(video_media.PublicURLBlocked):
            video_media.validate_public_url(url)


def test_validate_public_url_rejects_dns_result_containing_private_address(monkeypatch) -> None:
    def mixed_dns_results(*_args, **_kwargs):
        return [
            (2, 1, 6, "", ("8.8.8.8", 0)),
            (2, 1, 6, "", ("127.0.0.1", 0)),
        ]

    monkeypatch.setattr(video_media.socket, "getaddrinfo", mixed_dns_results)

    with pytest.raises(video_media.PublicURLBlocked):
        video_media.validate_public_url("https://cdn.example.test/video.mp4")


def test_fetch_revalidates_every_redirect_target(monkeypatch) -> None:
    monkeypatch.setattr(
        video_media,
        "_host_is_public",
        lambda hostname: hostname.rstrip(".").lower() == "cdn.example.test",
    )
    redirect = _FakeResponse(
        status=302,
        headers={"Location": "http://127.0.0.1/private.mp4"},
    )
    opener = _QueueOpener(redirect)

    with pytest.raises(video_media.PublicURLBlocked):
        video_media.fetch_public_resource(
            "https://cdn.example.test/video.mp4",
            max_bytes=64,
            opener=opener,
        )

    assert len(opener.requests) == 1
    assert redirect.closed


def test_default_opener_is_built_with_a_redirect_handler_instance(monkeypatch) -> None:
    captured = []

    def fake_build_opener(*handlers):
        captured.extend(handlers)
        return SimpleNamespace(open=lambda *_args, **_kwargs: None)

    monkeypatch.setattr(video_media, "build_opener", fake_build_opener)

    opener = video_media._default_opener()

    assert opener is not None
    assert captured
    assert any(isinstance(handler, HTTPRedirectHandler) for handler in captured)
    assert all(not isinstance(handler, type) for handler in captured)


def test_fetch_rejects_oversized_content_length_without_reading() -> None:
    response = _FakeResponse(
        b"body that must never be read",
        headers={"Content-Length": "65"},
    )
    opener = _QueueOpener(response)

    with pytest.raises(video_media.VideoMediaError) as error:
        video_media.fetch_public_resource(
            "https://8.8.8.8/video.mp4",
            max_bytes=64,
            opener=opener,
        )

    assert error.value.status_code == 413
    assert response.read_calls == 0
    assert response.closed


def test_fetch_enforces_size_when_content_length_is_missing_or_untrusted() -> None:
    response = _FakeResponse(
        b"x" * 65,
        headers={"Content-Length": "not-a-number"},
    )
    opener = _QueueOpener(response)

    with pytest.raises(video_media.VideoMediaError) as error:
        video_media.fetch_public_resource(
            "https://8.8.8.8/video.mp4",
            max_bytes=64,
            opener=opener,
        )

    assert error.value.status_code == 413
    assert response.read_calls >= 1
    assert response.closed


def test_fetch_rejects_gzip_payload_after_bounded_expansion() -> None:
    compressed = gzip.compress(b"expanded video bytes" * 100)
    assert len(compressed) < 64
    response = _FakeResponse(
        compressed,
        headers={"Content-Encoding": "gzip"},
    )
    opener = _QueueOpener(response)

    with pytest.raises(video_media.VideoMediaError) as error:
        video_media.fetch_public_resource(
            "https://8.8.8.8/video.mp4",
            max_bytes=64,
            opener=opener,
        )

    assert error.value.status_code == 413
    assert response.closed


@pytest.mark.parametrize(
    ("filename", "expected"),
    (("dish.MP4", ".mp4"), ("dish.mov", ".mov")),
)
def test_safe_upload_extension_accepts_only_supported_video_containers(filename, expected) -> None:
    assert video_media.safe_upload_extension(filename) == expected


@pytest.mark.parametrize("filename", ("dish.m4a", "dish.mp3", "dish.mp4.exe", "bad\x00.mp4"))
def test_safe_upload_extension_rejects_audio_and_malformed_names(filename) -> None:
    with pytest.raises(video_media.VideoMediaError):
        video_media.safe_upload_extension(filename)


def _fake_probe_run(stdout: str, calls=None):
    def run(*_args, **_kwargs):
        if calls is not None:
            calls.append(_args[0])
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    return run


def test_probe_rejects_reported_format_that_does_not_match_mp4_or_mov(tmp_path, monkeypatch) -> None:
    path = tmp_path / "dish.mp4"
    path.write_bytes(b"container bytes")
    calls = []
    monkeypatch.setattr(
        video_media.subprocess,
        "run",
        _fake_probe_run("format_name=avi\nduration=12.0\ncodec_type=video\n", calls),
    )

    with pytest.raises(video_media.VideoMediaError) as error:
        video_media.probe_video_file(path, ffprobe_path="ffprobe", ffmpeg_path="")

    assert error.value.status_code == 415
    assert any("stream=codec_type" in argument for argument in calls[0])


def test_probe_rejects_audio_only_mov_container_even_with_mp4_extension(tmp_path, monkeypatch) -> None:
    path = tmp_path / "dish.mp4"
    path.write_bytes(b"audio container bytes")
    monkeypatch.setattr(
        video_media.subprocess,
        "run",
        _fake_probe_run("format_name=mov\nduration=12.0\ncodec_type=audio\n"),
    )

    with pytest.raises(video_media.VideoMediaError) as error:
        video_media.probe_video_file(path, ffprobe_path="ffprobe", ffmpeg_path="")

    assert error.value.status_code == 415


def test_probe_rejects_nan_duration_as_invalid_media(tmp_path, monkeypatch) -> None:
    path = tmp_path / "dish.mp4"
    path.write_bytes(b"container bytes")
    monkeypatch.setattr(
        video_media.subprocess,
        "run",
        _fake_probe_run("format_name=mp4\nduration=nan\ncodec_type=video\n"),
    )

    with pytest.raises(video_media.VideoMediaError) as error:
        video_media.probe_video_file(path, ffprobe_path="ffprobe", ffmpeg_path="")

    assert error.value.status_code == 415


def test_probe_rejects_video_longer_than_three_minutes(tmp_path, monkeypatch) -> None:
    path = tmp_path / "dish.mp4"
    path.write_bytes(b"container bytes")
    monkeypatch.setattr(
        video_media.subprocess,
        "run",
        _fake_probe_run("format_name=mp4\nduration=180.1\ncodec_type=video\n"),
    )

    with pytest.raises(video_media.VideoMediaError) as error:
        video_media.probe_video_file(path, ffprobe_path="ffprobe", ffmpeg_path="")

    assert error.value.status_code == 413


def test_probe_uses_ffmpeg_fallback_when_ffprobe_is_unavailable(tmp_path, monkeypatch) -> None:
    path = tmp_path / "dish.mov"
    path.write_bytes(b"container bytes")
    calls = []

    def fake_run(args, **_kwargs):
        calls.append(args)
        return SimpleNamespace(
            returncode=1,
            stdout="",
            stderr=(
                "Input #0, mov,mp4,m4a,3gp,3g2,mj2, from 'dish.mov':\n"
                "  Duration: 00:02:00.00, start: 0.000000, bitrate: 100 kb/s\n"
                "    Stream #0:0: Video: h264 (High), yuv420p, 1920x1080\n"
            ),
        )

    monkeypatch.setattr(video_media.subprocess, "run", fake_run)

    info = video_media.probe_video_file(path, ffprobe_path="", ffmpeg_path="ffmpeg")

    assert info.format_name == "mov"
    assert info.duration_seconds == 120.0
    assert calls and calls[0] == ["ffmpeg", "-hide_banner", "-i", str(path)]


def test_probe_rejects_audio_only_ffmpeg_metadata(tmp_path, monkeypatch) -> None:
    path = tmp_path / "dish.mov"
    path.write_bytes(b"audio container bytes")
    monkeypatch.setattr(
        video_media.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=1,
            stdout="",
            stderr=(
                "Input #0, mov,mp4,m4a,3gp,3g2,mj2, from 'dish.mov':\n"
                "  Duration: 00:02:00.00, start: 0.000000, bitrate: 100 kb/s\n"
                "    Stream #0:0: Audio: aac, 48000 Hz, stereo\n"
            ),
        ),
    )

    with pytest.raises(video_media.VideoMediaError) as error:
        video_media.probe_video_file(path, ffprobe_path="", ffmpeg_path="ffmpeg")

    assert error.value.status_code == 415


def test_cleanup_stale_temp_dirs_is_scoped_to_importer_directories(tmp_path) -> None:
    owned = tmp_path / "video-import-stale"
    owned.mkdir()
    (owned / "temporary.mp4").write_bytes(b"stale")
    (owned / video_media._TEMP_OWNER_MARKER).write_text(str(2**31 - 1), encoding="ascii")

    active = tmp_path / "video-import-active"
    active.mkdir()
    (active / video_media._TEMP_OWNER_MARKER).write_text(str(os.getpid()), encoding="ascii")

    unrelated = tmp_path / "other-work"
    unrelated.mkdir()
    sentinel = unrelated / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")

    nested = tmp_path / "project"
    nested.mkdir()
    nested_sentinel = nested / "keep.txt"
    nested_sentinel.write_text("keep", encoding="utf-8")

    assert video_media.cleanup_stale_temp_dirs(tmp_path) == 1
    assert not owned.exists()
    assert active.exists()
    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert nested_sentinel.read_text(encoding="utf-8") == "keep"


def test_create_task_temp_dir_marks_current_pid_and_cleanup_preserves_it(tmp_path) -> None:
    first = video_media.create_task_temp_dir(tmp_path, "service-one")
    second = video_media.create_task_temp_dir(tmp_path, "service-two")

    assert (first / video_media._TEMP_OWNER_MARKER).read_text(encoding="ascii").strip() == str(os.getpid())
    assert (second / video_media._TEMP_OWNER_MARKER).read_text(encoding="ascii").strip() == str(os.getpid())
    assert video_media.cleanup_stale_temp_dirs(tmp_path) == 0
    assert first.exists() and second.exists()


def test_cleanup_does_not_follow_importer_named_symlink(tmp_path) -> None:
    target = tmp_path / "outside"
    target.mkdir()
    sentinel = target / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")
    link = tmp_path / "video-import-link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are unavailable in this test environment")

    assert video_media.cleanup_stale_temp_dirs(tmp_path) == 0
    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert link.is_symlink()
