"""Asynchronous video-to-recipe import service.

The service is intentionally independent from the HTTP console.  The console
can submit a link or upload, poll the snapshot, edit the review draft and then
confirm the recipe for the existing kitchen session to consume.
"""

from __future__ import annotations

import base64
import hashlib
import inspect
import json
import math
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener

from .video_media import (
    MAX_MODEL_BASE64_BYTES,
    MAX_VIDEO_BYTES,
    MAX_VIDEO_SECONDS,
    MediaInfo,
    VideoMediaError,
    cleanup_stale_temp_dirs,
    create_task_temp_dir,
    download_public_video,
    fetch_public_resource,
    model_payload_size,
    probe_video_file,
    safe_upload_extension,
    validate_public_url,
    write_upload,
)
from .video_recipe_store import (
    DEFAULT_IMPORTED_RECIPE_DIR,
    VideoRecipeStore,
    VideoRecipeStoreError,
    scale_recipe,
)


MAX_TASK_SECONDS = 5 * 60
MAX_SHARE_TEXT_LENGTH = 20_000
MAX_DRAFT_TEXT_LENGTH = 2_000
MAX_DRAFT_LIST_ITEMS = 64
MAX_HTML_BYTES = 8 * 1024 * 1024
VIDEO_MODEL_DEFAULT = "qwen3-omni-flash"
VIDEO_MODEL_TIMEOUT_SECONDS = 90.0
VIDEO_MODEL_MAX_OUTPUT_TOKENS = 3000
DRAFT_COMPLETION_TIMEOUT_SECONDS = 40.0
PARTIAL_PREVIEW_INTERVAL_SECONDS = 0.5
PARTIAL_PREVIEW_MAX_TEXT = 48_000
SUPPORTED_XHS_HOSTS = {
    "xhslink.cn",
    "xhslink.com",
    "xiaohongshu.com",
    "www.xhslink.cn",
    "www.xhslink.com",
    "www.xiaohongshu.com",
}
_URL_RE = re.compile(r"https?://[^\s<>\"'）)】》]+", flags=re.IGNORECASE)
_NOTE_ID_RE = re.compile(r"/(?:explore|discovery/item|item)/([A-Za-z0-9_-]{8,})", flags=re.IGNORECASE)
_SUPPORTED_DRAFT_FIELDS = {
    "name",
    "title",
    "servings",
    "ingredients",
    "equipment",
    "steps",
    "estimated_time_minutes",
    "estimated_minutes",
    "difficulty",
    "notes",
    "safety_notes",
}
_STEP_FIELDS = {
    "step_number",
    "instruction",
    "duration_seconds",
    "heat_level",
    "safety_note",
    "display_text",
    "robot_action",
    "led_effect",
    "expression",
}
_INGREDIENT_FIELDS = {"name", "amount", "unit", "optional"}


class VideoImportError(RuntimeError):
    """Public import failure whose message is safe to expose to a console."""

    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(str(message))
        self.status_code = int(status_code)


class VideoImportCancelled(RuntimeError):
    pass


@dataclass
class VideoSource:
    video_url: str | None = None
    title: str = ""
    description: str = ""
    duration_seconds: float | None = None
    source_url: str | None = None
    platform: str = ""
    note_id: str | None = None
    author: str | None = None
    video_bytes: bytes | None = None
    video_path: Path | None = None
    content_type: str | None = None
    # Keep the user supplied link (without query credentials) separately from
    # the canonical note URL.  Signed media URLs and xsec query tokens are
    # request-only and never enter this field.
    original_url: str | None = None

    def safe_metadata(self) -> dict[str, Any]:
        description = re.sub(r"https?://[^\s<>\"']+", "[链接]", self.description or "")
        return {
            "platform": self.platform or "video",
            "source_url": self.original_url or self.source_url,
            "canonical_note_url": self.source_url,
            "note_id": self.note_id,
            "title": self.title,
            "author": self.author,
            "description": description[:1000],
        }


@dataclass
class _ImportTask:
    task_id: str
    source_kind: str
    source_key: str
    share_text: str | None = None
    upload_filename: str | None = None
    upload_data: bytes | None = None
    stage: str = "queued"
    failed_stage: str | None = None
    message: str = "已收到视频，准备处理。"
    draft: dict[str, Any] | None = None
    error: str | None = None
    source: VideoSource | None = None
    media_info: MediaInfo | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event)
    temp_dir: Path | None = None
    future: Any | None = None
    started_at: float | None = None
    deadline: float | None = None
    timed_out: bool = False
    submitted_at: float | None = None
    metric_phase: str | None = None
    metric_phase_started_at: float | None = None
    stage_seconds: dict[str, float] = field(
        default_factory=lambda: {"fetching": 0.0, "analyzing": 0.0, "structuring": 0.0}
    )
    first_preview_at: float | None = None
    first_step_at: float | None = None
    partial_preview: dict[str, Any] | None = None
    partial_preview_updated_at: float | None = None
    model_requests: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    compression_seconds: float = 0.0
    model_seconds: float = 0.0
    finished_at: float | None = None
    draft_revision: int = 0
    completion_attempted: bool = False
    completion_in_progress: bool = False
    lock: threading.RLock = field(default_factory=threading.RLock)


def _safe_text(value: Any, *, limit: int = MAX_DRAFT_TEXT_LENGTH) -> str:
    text = str(value or "").strip()
    return text[:limit]


def _as_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return float(value)
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def extract_share_url(share_text: str) -> str:
    if not isinstance(share_text, str):
        raise VideoImportError("分享内容必须是文字。", status_code=400)
    if not share_text.strip() or len(share_text) > MAX_SHARE_TEXT_LENGTH:
        raise VideoImportError("分享内容无效或过长。", status_code=400)
    matches = _URL_RE.findall(share_text)
    if not matches:
        raise VideoImportError("请粘贴包含视频链接的分享内容。", status_code=400)
    for candidate in matches:
        cleaned = candidate.rstrip("。，、；;,.!?！？]}>\"'")
        parsed = urlsplit(cleaned)
        host = (parsed.hostname or "").lower().rstrip(".")
        if host in SUPPORTED_XHS_HOSTS:
            return cleaned
    raise VideoImportError("暂只支持小红书视频分享链接，请上传视频文件。", status_code=415)


def canonical_note_url(note_id: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9_-]", "", str(note_id or ""))
    if not clean:
        return ""
    return f"https://www.xiaohongshu.com/explore/{clean}"


def _safe_source_link(url: str) -> str | None:
    """Return an allowed source link with credentials/query tokens removed."""

    try:
        parsed = urlsplit(str(url or ""))
    except ValueError:
        return None
    host = (parsed.hostname or "").lower().rstrip(".")
    if host not in SUPPORTED_XHS_HOSTS or parsed.scheme.lower() not in {"http", "https"}:
        return None
    # Preserve a short-link path users can open again while dropping xsec and
    # other share query tokens from persisted metadata.
    return f"{parsed.scheme.lower()}://{host}{parsed.path or '/'}"


def _source_key_from_url(url: str) -> str:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    path = re.sub(r"/+", "/", parsed.path or "/").rstrip("/") or "/"
    # Strip all query tokens.  Short-link security tokens are request-only and
    # must not become persisted metadata or deduplication data.
    return hashlib.sha256(f"{host}{path}".encode("utf-8")).hexdigest()


def _canonical_source_from_note(note: Mapping[str, Any], fallback_url: str) -> tuple[str, str | None]:
    note_id = str(note.get("noteId") or note.get("note_id") or note.get("id") or "").strip()
    if not note_id:
        match = _NOTE_ID_RE.search(fallback_url)
        note_id = match.group(1) if match else ""
    return canonical_note_url(note_id), (note_id or None)


def _extract_balanced_object(text: str, marker: str) -> str | None:
    """Extract one JS object after ``marker`` without evaluating JavaScript."""

    start_marker = text.find(marker)
    if start_marker < 0:
        start_marker = text.lower().find(marker.lower())
    if start_marker < 0:
        return None
    start = text.find("{", start_marker + len(marker))
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    quote = ""
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                in_string = False
            continue
        if char in {"\"", "'"}:
            in_string = True
            quote = char
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def _replace_undefined_json_tokens(text: str) -> str:
    """Replace bare JS ``undefined`` values while leaving string text intact."""

    output: list[str] = []
    index = 0
    in_string = False
    escaped = False
    quote = ""
    while index < len(text):
        char = text[index]
        if in_string:
            output.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                in_string = False
            index += 1
            continue
        if char in {"\"", "'"}:
            in_string = True
            quote = char
            output.append(char)
            index += 1
            continue
        if text.startswith("undefined", index):
            previous = text[index - 1] if index else ""
            following_index = index + len("undefined")
            following = text[following_index] if following_index < len(text) else ""
            if (not previous or not (previous.isalnum() or previous in "_$")) and (
                not following or not (following.isalnum() or following in "_$")
            ):
                output.append("null")
                index = following_index
                continue
        output.append(char)
        index += 1
    return "".join(output)


def _decode_json_object(text: str) -> dict[str, Any] | None:
    candidate = _replace_undefined_json_tokens(text).strip()
    try:
        value = json.loads(candidate)
    except (TypeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _walk_dicts(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_dicts(child)


def _find_note_data(state: dict[str, Any]) -> dict[str, Any] | None:
    candidates = []
    for value in _walk_dicts(state):
        if not isinstance(value.get("video"), dict):
            continue
        if any(value.get(key) for key in ("title", "noteId", "note_id", "id")):
            candidates.append(value)
    if candidates:
        return max(candidates, key=lambda item: int(bool(item.get("noteId"))) + int(bool(item.get("title"))))
    return None


def _iter_video_streams(note: Mapping[str, Any]) -> Iterable[Mapping[str, Any]]:
    video = note.get("video")
    media = video.get("media") if isinstance(video, dict) else None
    stream = media.get("stream") if isinstance(media, dict) else None
    if not isinstance(stream, dict):
        return
    preferred = []
    for key in ("h264", "h265", "av1"):
        rows = stream.get(key)
        if isinstance(rows, list):
            preferred.extend(row for row in rows if isinstance(row, dict))
    yield from preferred


def _pick_video_url(note: Mapping[str, Any]) -> tuple[str | None, float | None]:
    duration: float | None = None
    video = note.get("video")
    media = video.get("media") if isinstance(video, dict) else None
    nested_video = media.get("video") if isinstance(media, dict) else None
    if isinstance(nested_video, dict):
        duration = _as_number(nested_video.get("duration"))
    for stream in _iter_video_streams(note):
        stream_duration = _as_number(stream.get("videoDuration") or stream.get("video_duration") or stream.get("duration"))
        if stream_duration is not None:
            # XHS page state reports stream duration in milliseconds while the
            # compact media.video field reports seconds.
            if stream_duration > 1000:
                stream_duration /= 1000
            duration = duration or stream_duration
        for key in ("masterUrl", "master_url", "url", "playUrl", "play_url"):
            candidate = str(stream.get(key) or "").strip()
            if candidate:
                return candidate, duration
        for key in ("backupUrls", "backup_urls"):
            backups = stream.get(key)
            if isinstance(backups, list):
                for candidate in backups:
                    if str(candidate or "").strip():
                        return str(candidate).strip(), duration
    return None, duration


def parse_xiaohongshu_html(html: str, *, source_url: str = "") -> VideoSource:
    """Parse the public mobile H5 state without executing page JavaScript."""

    if not isinstance(html, str) or not html.strip() or len(html.encode("utf-8")) > MAX_HTML_BYTES:
        raise VideoImportError("小红书公开页面内容无效，请上传视频文件。", status_code=502)
    state: dict[str, Any] | None = None
    for marker in ("window.__INITIAL_STATE__", "__INITIAL_STATE__", "window.__INITIAL_STATE__ ="):
        blob = _extract_balanced_object(html, marker)
        if blob:
            state = _decode_json_object(blob)
            if state:
                break
    if state is None:
        # Some test fixtures provide the state object directly, which is also
        # a safe input to accept because no code is evaluated.
        stripped = html.strip()
        if stripped.startswith("{"):
            state = _decode_json_object(stripped)
    if state is None:
        raise VideoImportError("未找到小红书视频信息，请上传视频文件。", status_code=502)
    note = _find_note_data(state)
    if note is None:
        raise VideoImportError("该分享内容不是可导入的视频，请上传视频文件。", status_code=422)
    video_url, duration = _pick_video_url(note)
    if not video_url:
        raise VideoImportError("该笔记没有可读取的视频，请上传视频文件。", status_code=422)
    try:
        validate_public_url(video_url)
    except VideoMediaError as exc:
        raise VideoImportError("视频地址不受支持，请上传视频文件。", status_code=400) from exc
    canonical, note_id = _canonical_source_from_note(note, source_url)
    user = note.get("user") if isinstance(note.get("user"), dict) else {}
    return VideoSource(
        video_url=video_url,
        title=_safe_text(note.get("title"), limit=240),
        description=_safe_text(note.get("desc") or note.get("description"), limit=1200),
        duration_seconds=duration,
        source_url=canonical or None,
        platform="xiaohongshu",
        note_id=note_id,
        author=_safe_text(user.get("nickName") or user.get("nickname"), limit=120),
        original_url=_safe_source_link(source_url),
    )


class PublicXiaohongshuAdapter:
    """Use the public mobile H5 page with a normal mobile User-Agent.

    The adapter never reads cookies, signs requests or attempts CAPTCHA/login
    bypasses.  It only parses the public ``__INITIAL_STATE__`` object and
    returns a short-lived signed media URL for the worker to download.
    """

    platform = "xiaohongshu"

    def __init__(
        self,
        *,
        fetcher: Callable[..., Any] | None = None,
        opener: Any | None = None,
        timeout: float = 20.0,
    ) -> None:
        self.fetcher = fetcher
        self.opener = opener
        self.timeout = timeout

    def can_handle(self, share_text: str) -> bool:
        try:
            extract_share_url(share_text)
        except VideoImportError:
            return False
        return True

    def resolve(self, share_text: str) -> VideoSource:
        share_url = extract_share_url(share_text)
        if self.fetcher is not None:
            try:
                result = self.fetcher(share_url)
            except TypeError:
                result = self.fetcher(share_url, {"User-Agent": self.user_agent})
            if isinstance(result, VideoSource):
                return result
            if isinstance(result, Mapping):
                if result.get("html") is not None:
                    return parse_xiaohongshu_html(str(result["html"]), source_url=share_url)
                source = _source_from_mapping(result, fallback_url=share_url, platform=self.platform)
                source.original_url = _safe_source_link(share_url)
                return source
            return parse_xiaohongshu_html(str(result), source_url=share_url)
        try:
            resource = fetch_public_resource(
                share_url,
                max_bytes=MAX_HTML_BYTES,
                headers={"User-Agent": self.user_agent, "Accept-Language": "zh-CN,zh;q=0.9"},
                timeout=self.timeout,
                opener=self.opener,
            )
        except VideoMediaError as exc:
            raise VideoImportError("小红书公开页面暂时无法访问，请上传视频文件。", status_code=502) from exc
        try:
            html = resource.data.decode("utf-8", errors="replace")
        except Exception as exc:
            raise VideoImportError("小红书公开页面内容无效，请上传视频文件。", status_code=502) from exc
        source = parse_xiaohongshu_html(html, source_url=resource.final_url or share_url)
        source.original_url = _safe_source_link(share_url)
        return source

    @property
    def user_agent(self) -> str:
        return "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148"

    # Names used by lightweight adapters in external integrations.
    fetch = resolve
    acquire = resolve
    resolve_share_text = resolve
    parse = resolve


def _source_from_mapping(mapping: Mapping[str, Any], *, fallback_url: str, platform: str) -> VideoSource:
    local_path_value = mapping.get("video_path") or mapping.get("path") or mapping.get("file_path")
    local_path = Path(str(local_path_value)) if local_path_value else None
    video_url = mapping.get("video_url") or mapping.get("videoUrl") or mapping.get("play_url") or mapping.get("playUrl")
    if not video_url:
        for key in ("masterUrl", "master_url", "url"):
            if mapping.get(key):
                video_url = mapping[key]
                break
    source_url = str(mapping.get("source_url") or mapping.get("sourceUrl") or "")
    note_id = str(mapping.get("note_id") or mapping.get("noteId") or "") or None
    canonical = canonical_note_url(note_id) if note_id else source_url or fallback_url
    if not video_url and local_path is None and not isinstance(mapping.get("video_bytes"), (bytes, bytearray)):
        raise VideoImportError("没有找到可下载的视频，请上传视频文件。", status_code=422)
    if video_url:
        try:
            validate_public_url(str(video_url))
        except VideoMediaError as exc:
            raise VideoImportError("视频地址不受支持，请上传视频文件。", status_code=400) from exc
    return VideoSource(
        video_url=str(video_url),
        title=_safe_text(mapping.get("title") or mapping.get("name"), limit=240),
        description=_safe_text(mapping.get("description") or mapping.get("desc"), limit=1200),
        duration_seconds=_as_number(mapping.get("duration_seconds") or mapping.get("duration")),
        source_url=canonical,
        platform=platform,
        note_id=note_id,
        author=_safe_text(mapping.get("author"), limit=120),
        video_bytes=bytes(mapping["video_bytes"]) if isinstance(mapping.get("video_bytes"), (bytes, bytearray)) else None,
        content_type=str(mapping.get("content_type") or "") or None,
        video_path=local_path,
        original_url=_safe_source_link(str(mapping.get("original_url") or fallback_url)),
    )


class WellbyteXiaohongshuAdapter:
    """Optional authorized Wellbyte API adapter.

    It is never used without ``VIDEO_IMPORT_API_KEY``.  The endpoint is
    configurable so deployments can select the exact Wellbyte account route
    documented for them without changing this runtime.
    """

    platform = "xiaohongshu"
    DEFAULT_ENDPOINT = "https://www.wellbyte.net/api/xiaohongshu/note/video_detail"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        endpoint: str | None = None,
        fetcher: Callable[..., Any] | None = None,
        timeout: float = 20.0,
    ) -> None:
        self.api_key = api_key if api_key is not None else os.getenv("VIDEO_IMPORT_API_KEY")
        self.endpoint = endpoint or os.getenv("VIDEO_IMPORT_WELLBYTE_URL", self.DEFAULT_ENDPOINT)
        self.fetcher = fetcher
        self.timeout = timeout

    @property
    def is_configured(self) -> bool:
        return bool(str(self.api_key or "").strip())

    def resolve(self, share_text: str) -> VideoSource:
        share_url = extract_share_url(share_text)
        if not self.is_configured:
            raise VideoImportError("当前链接通道未配置，请直接上传视频文件。", status_code=503)
        if self.fetcher is not None:
            try:
                result = self.fetcher(share_url, self.api_key)
            except TypeError:
                result = self.fetcher(share_url)
            if isinstance(result, VideoSource):
                return result
            if not isinstance(result, Mapping):
                try:
                    result = json.loads(str(result))
                except (TypeError, json.JSONDecodeError) as exc:
                    raise VideoImportError("链接通道返回内容无效，请上传视频文件。", status_code=502) from exc
            return self._parse_response(result, share_url)
        try:
            endpoint = validate_public_url(self.endpoint)
        except VideoMediaError as exc:
            raise VideoImportError("链接通道配置无效，请上传视频文件。", status_code=503) from exc
        body = json.dumps({"url": share_url}, ensure_ascii=False).encode("utf-8")
        request = Request(
            endpoint,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": f"Bearer {self.api_key}",
                "X-API-Key": str(self.api_key),
                "User-Agent": "KitchenConsole/1.0",
            },
        )
        try:
            opener = build_opener()
            response = opener.open(request, timeout=self.timeout)
            data = response.read(MAX_HTML_BYTES + 1)
            response.close()
        except (HTTPError, URLError, OSError, TimeoutError) as exc:
            raise VideoImportError("链接通道暂时无法访问，请上传视频文件。", status_code=502) from exc
        if len(data) > MAX_HTML_BYTES:
            raise VideoImportError("链接通道返回内容过大，请上传视频文件。", status_code=502)
        try:
            payload = json.loads(data.decode("utf-8", errors="replace"))
        except (TypeError, json.JSONDecodeError) as exc:
            raise VideoImportError("链接通道返回内容无效，请上传视频文件。", status_code=502) from exc
        if not isinstance(payload, Mapping):
            raise VideoImportError("链接通道返回内容无效，请上传视频文件。", status_code=502)
        return self._parse_response(payload, share_url)

    def _parse_response(self, payload: Mapping[str, Any], fallback_url: str) -> VideoSource:
        candidate: Mapping[str, Any] = payload
        for key in ("data", "result", "note", "video"):
            nested = candidate.get(key)
            if isinstance(nested, Mapping):
                candidate = nested
        # Some APIs nest a stream row inside data.video.stream.
        if isinstance(candidate.get("stream"), Mapping):
            stream = candidate["stream"]
            candidate = {**candidate, **stream}
        for key in ("video_url", "videoUrl", "play_url", "playUrl", "masterUrl", "master_url", "url"):
            if candidate.get(key):
                return _source_from_mapping(candidate, fallback_url=fallback_url, platform=self.platform)
        for value in _walk_dicts(payload):
            if any(value.get(key) for key in ("video_url", "videoUrl", "play_url", "playUrl", "masterUrl", "master_url")):
                return _source_from_mapping(value, fallback_url=fallback_url, platform=self.platform)
        raise VideoImportError("链接通道没有返回可下载视频，请上传视频文件。", status_code=422)

    fetch = resolve
    acquire = resolve
    resolve_share_text = resolve


class CompositeXiaohongshuAdapter:
    """Public H5 first, then the optional authorized API."""

    def __init__(self, public: PublicXiaohongshuAdapter | None = None, wellbyte: WellbyteXiaohongshuAdapter | None = None) -> None:
        self.public = public or PublicXiaohongshuAdapter()
        self.wellbyte = wellbyte or WellbyteXiaohongshuAdapter()

    def resolve(self, share_text: str) -> VideoSource:
        public_error: VideoImportError | None = None
        try:
            return self.public.resolve(share_text)
        except VideoImportError as exc:
            public_error = exc
        if self.wellbyte.is_configured:
            try:
                return self.wellbyte.resolve(share_text)
            except VideoImportError:
                pass
        if public_error is not None:
            # Make the upload fallback explicit while preserving safe status.
            raise VideoImportError(
                "无法从公开页面读取该视频，请直接上传视频文件。",
                status_code=public_error.status_code if public_error.status_code >= 400 else 502,
            ) from public_error
        raise VideoImportError("无法读取该链接，请上传视频文件。", status_code=502)

    fetch = resolve
    acquire = resolve


class QwenVideoModel:
    """Small video Chat Completions client kept separate from qwen_client.py."""

    def __init__(
        self,
        *,
        client: Any | None = None,
        config: Any | None = None,
        model: str | None = None,
        timeout: float | None = None,
        max_tokens: int = VIDEO_MODEL_MAX_OUTPUT_TOKENS,
    ) -> None:
        self._client = client
        self.config = config or _load_qwen_config()
        self.model = str(model or os.getenv("QWEN_VIDEO_MODEL", VIDEO_MODEL_DEFAULT))
        configured_timeout = VIDEO_MODEL_TIMEOUT_SECONDS
        timeout_value: Any = timeout if timeout is not None else os.getenv("QWEN_VIDEO_TIMEOUT_SECONDS")
        if timeout_value is None:
            timeout_value = configured_timeout
        try:
            self.timeout = max(0.1, float(timeout_value))
        except (TypeError, ValueError):
            self.timeout = float(configured_timeout or 20.0)
        self.max_tokens = max_tokens
        self.api_key = getattr(self.config, "api_key", None)
        self.base_url = getattr(self.config, "base_url", None)
        self.text_model = str(
            getattr(self.config, "text_model", None)
            or os.getenv("QWEN_TEXT_MODEL")
            or self.model
        )
        self.max_retries = getattr(self.config, "max_retries", 0)
        self.last_usage: dict[str, int] | None = None

    def is_available(self) -> bool:
        return bool(self.api_key or self._client is not None)

    def analyze_video(
        self,
        video_data_url: str,
        prompt: str,
        *,
        partial_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        if not isinstance(video_data_url, str) or not video_data_url.startswith("data:video/"):
            raise VideoImportError("视频输入格式无效。", status_code=400)
        if not self.is_available() and self._client is None:
            raise VideoImportError("未配置视频分析服务，请直接上传视频或配置千问 Key。", status_code=503)
        return self._complete(
            [
                {"type": "video_url", "video_url": {"url": video_data_url}},
                {"type": "text", "text": str(prompt)},
            ],
            partial_callback=partial_callback,
        )

    def organize_text(
        self,
        evidence_text: str,
        prompt: str,
        *,
        partial_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """Organize multi-segment evidence without sending the video again."""

        if not self.is_available() and self._client is None:
            raise VideoImportError("未配置视频分析服务，请直接上传视频或配置千问 Key。", status_code=503)
        bounded_evidence = str(evidence_text or "")[:PARTIAL_PREVIEW_MAX_TEXT]
        return self._complete(
            [{"type": "text", "text": f"{str(prompt)}\n证据 JSON：{bounded_evidence}"}],
            partial_callback=partial_callback,
        )

    def complete_recipe(
        self,
        recipe: Mapping[str, Any],
        missing_fields: list[str],
        quality_issues: list[str] | None = None,
        *,
        title: str = "",
        partial_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """Fill only missing recipe fields with a text-only Qwen request."""

        payload = {
            "title": _safe_text(title, limit=160),
            "missing_fields": [_safe_text(item, limit=160) for item in missing_fields[:64]],
            "quality_issues": [_safe_text(item, limit=240) for item in (quality_issues or [])[:32]],
            "recipe": deepcopy(dict(recipe)) if isinstance(recipe, Mapping) else {},
        }
        prompt = _recipe_completion_prompt(payload)
        return self._complete(
            [{"type": "text", "text": prompt}],
            partial_callback=partial_callback,
            model_override=self.text_model,
        )

    def _complete(
        self,
        content: list[dict[str, Any]],
        *,
        partial_callback: Callable[[dict[str, Any]], None] | None = None,
        model_override: str | None = None,
    ) -> dict[str, Any]:
        request = {
            "model": str(model_override or self.model),
            "messages": [{"role": "user", "content": content}],
            # Qwen Omni video requests require a text modality and streaming;
            # stream_options also lets the API return aggregate token usage in
            # its final empty-choice chunk without adding another request.
            "stream": True,
            "stream_options": {"include_usage": True},
            "modalities": ["text"],
            "temperature": 0,
            "max_completion_tokens": self.max_tokens,
            "timeout": self.timeout,
            "extra_body": {"enable_thinking": False},
        }
        self.last_usage = None
        try:
            response = self._get_client().chat.completions.create(**request)
            usage: dict[str, int] = {}
            text = _response_content(response, partial_callback=partial_callback, usage_callback=usage)
            self.last_usage = usage or None
            parsed = _parse_json_content(text)
            if not isinstance(parsed, dict):
                raise ValueError("model output root")
            return parsed
        except VideoImportError:
            raise
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise VideoImportError("视频分析结果无效，请重试或上传视频文件。", status_code=502) from exc
        except Exception as exc:
            status = getattr(exc, "status_code", None) or getattr(exc, "status", None) or getattr(exc, "code", None)
            if str(status) in {"401", "403"} or getattr(exc, "response", None) is not None and str(getattr(getattr(exc, "response", None), "status_code", "")) in {"401", "403"}:
                body = getattr(exc, "body", None)
                if isinstance(body, Mapping):
                    error = body.get("error", body)
                    code = str(error.get("code", "")).lower() if isinstance(error, Mapping) else ""
                    if code in {"invalid_api_key", "invalidapikey"}:
                        raise VideoImportError("视频分析服务鉴权失败：千问 API Key 无效或已失效，请更新有效凭证后重试。", status_code=401) from exc
                raise VideoImportError("视频分析服务鉴权或模型权限不足，请检查千问 Key 和模型配置。", status_code=int(status or 403)) from exc
            raise VideoImportError("视频分析服务暂时不可用，请稍后重试。", status_code=502) from exc

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise VideoImportError("缺少视频分析依赖，请稍后重试。", status_code=503) from exc
        self._client = OpenAI(
            base_url=self.base_url,
            api_key=self.api_key,
            timeout=self.timeout,
            max_retries=self.max_retries,
        )
        return self._client


def _load_qwen_config() -> Any:
    try:
        skill_root = Path(__file__).resolve().parents[1] / "skills" / "kitchen_assistant"
        if str(skill_root) not in sys.path:
            sys.path.insert(0, str(skill_root))
        from llm.config import QwenConfig

        return QwenConfig.from_environment()
    except Exception:
        return type("QwenConfigFallback", (), {"api_key": os.getenv("DASHSCOPE_API_KEY"), "base_url": os.getenv("QWEN_BASE_URL"), "max_retries": 0, "vision_timeout": 20.0})()


def _response_content(
    response: Any,
    *,
    partial_callback: Callable[[dict[str, Any]], None] | None = None,
    usage_callback: Callable[[Mapping[str, Any]], None] | dict[str, int] | None = None,
) -> str:
    """Collect a model response while exposing bounded preview callbacks."""

    def collect_usage(value: Any) -> None:
        if usage_callback is None:
            return
        usage = value.get("usage") if isinstance(value, Mapping) else getattr(value, "usage", None)
        usage_map = _usage_mapping(usage)
        if not usage_map:
            return
        if isinstance(usage_callback, dict):
            for key in ("prompt_tokens", "completion_tokens", "input_tokens", "output_tokens", "total_tokens"):
                value = usage_map.get(key)
                if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)) and value >= 0:
                    usage_callback[key] = int(value)
        else:
            usage_callback(usage_map)

    def content_text(value: Any) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            return "".join(
                str(item.get("text") or "")
                for item in value
                if isinstance(item, Mapping)
            )
        return ""

    if hasattr(response, "__iter__") and not isinstance(response, (str, bytes, Mapping)) and not hasattr(response, "choices"):
        parts: list[str] = []
        saw_choice = False
        last_preview_at: float | None = None
        chars_since_preview = 0

        def emit_preview(*, force: bool = False) -> None:
            nonlocal last_preview_at, chars_since_preview
            if partial_callback is None or not parts:
                return
            now = time.monotonic()
            if not force and last_preview_at is not None and now - last_preview_at < PARTIAL_PREVIEW_INTERVAL_SECONDS:
                return
            if not force and chars_since_preview < 32:
                return
            # Keep callback parsing bounded even if a misbehaving provider
            # ignores max_completion_tokens.  The full response remains in
            # ``parts`` for final JSON validation below.
            preview_text = "".join(parts)[:PARTIAL_PREVIEW_MAX_TEXT]
            preview = _partial_recipe_preview(preview_text)
            if preview is not None:
                partial_callback(preview)
                last_preview_at = now
                chars_since_preview = 0

        for chunk in response:
            collect_usage(chunk)
            choices = chunk.get("choices") if isinstance(chunk, Mapping) else getattr(chunk, "choices", None)
            if not choices:
                continue
            saw_choice = True
            first = choices[0]
            delta = first.get("delta") if isinstance(first, Mapping) else getattr(first, "delta", None)
            content = delta.get("content") if isinstance(delta, Mapping) else getattr(delta, "content", None)
            piece = content_text(content)
            if piece:
                parts.append(piece)
                chars_since_preview += len(piece)
                emit_preview()
        if not saw_choice:
            raise VideoImportError("视频分析服务返回为空。", status_code=502)
        emit_preview(force=True)
        return "".join(parts).strip()
    collect_usage(response)
    choices = response.get("choices") if isinstance(response, Mapping) else getattr(response, "choices", None)
    if not choices:
        raise VideoImportError("视频分析服务返回为空。", status_code=502)
    first = choices[0]
    message = first.get("message") if isinstance(first, Mapping) else getattr(first, "message", None)
    content = message.get("content") if isinstance(message, Mapping) else getattr(message, "content", None)
    content = content_text(content)
    if not content.strip():
        raise VideoImportError("视频分析服务返回为空。", status_code=502)
    if partial_callback is not None:
        preview = _partial_recipe_preview(content[:PARTIAL_PREVIEW_MAX_TEXT])
        if preview is not None:
            partial_callback(preview)
    return content


def _usage_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    for method_name in ("model_dump", "dict", "to_dict"):
        method = getattr(value, method_name, None)
        if callable(method):
            try:
                dumped = method()
            except Exception:
                dumped = None
            if isinstance(dumped, Mapping):
                return dict(dumped)
    result: dict[str, Any] = {}
    for key in ("prompt_tokens", "completion_tokens", "input_tokens", "output_tokens", "total_tokens"):
        item = getattr(value, key, None)
        if item is not None:
            result[key] = item
    return result


def _partial_recipe_preview(text: str) -> dict[str, Any] | None:
    """Extract only complete JSON fields from a still-streaming response.

    This intentionally never closes braces or treats a partially typed JSON
    object as a recipe.  It only decodes complete ingredient/step objects and
    complete quoted name values, so a preview can be shown safely while the
    final response continues to stream.
    """

    bounded = str(text or "")[:PARTIAL_PREVIEW_MAX_TEXT]
    name_match = re.search(r'"(?:name|title)"\s*:\s*"((?:\\.|[^"\\])*)"', bounded)
    name = ""
    if name_match:
        try:
            name = _safe_text(json.loads(f'"{name_match.group(1)}"'), limit=120)
        except (TypeError, ValueError, json.JSONDecodeError):
            name = ""

    def complete_objects(key: str) -> list[dict[str, Any]]:
        marker = re.search(rf'"{re.escape(key)}"\s*:\s*\[', bounded)
        if marker is None:
            return []
        objects: list[dict[str, Any]] = []
        object_start: int | None = None
        depth = 0
        in_string = False
        escaped = False
        for index in range(marker.end(), len(bounded)):
            char = bounded[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
                continue
            if char == "{" and depth == 0:
                object_start = index
                depth = 1
                continue
            if char == "{" and depth > 0:
                depth += 1
                continue
            if char == "}" and depth > 0:
                depth -= 1
                if depth == 0 and object_start is not None:
                    try:
                        item = json.loads(bounded[object_start : index + 1])
                    except (TypeError, ValueError, json.JSONDecodeError):
                        item = None
                    if isinstance(item, dict):
                        objects.append(item)
                    object_start = None
                continue
            if char == "]" and depth == 0:
                break
        return objects[:MAX_DRAFT_LIST_ITEMS]

    ingredients: list[dict[str, Any]] = []
    for item in complete_objects("ingredients"):
        name_value = _safe_text(item.get("name"), limit=120)
        if not name_value:
            continue
        amount_value = item.get("amount")
        if isinstance(amount_value, bool) or not isinstance(amount_value, (str, int, float)):
            amount_value = ""
        elif isinstance(amount_value, str):
            amount_value = amount_value[:80]
        compact: dict[str, Any] = {
            "name": name_value,
            "amount": amount_value,
            "unit": _safe_text(item.get("unit"), limit=40),
        }
        ingredients.append(compact)

    steps: list[dict[str, Any]] = []
    for item in complete_objects("steps"):
        instruction = _safe_text(item.get("instruction"), limit=MAX_DRAFT_TEXT_LENGTH)
        if not instruction:
            continue
        compact_step: dict[str, Any] = {"instruction": instruction}
        duration = item.get("duration_seconds")
        if isinstance(duration, (int, float)) and not isinstance(duration, bool) and math.isfinite(float(duration)) and duration > 0:
            compact_step["duration_seconds"] = duration
        else:
            compact_step["duration_seconds"] = None
        heat = item.get("heat_level")
        compact_step["heat_level"] = _safe_text(heat, limit=40) if heat is not None else None
        steps.append(compact_step)

    if not name and not ingredients and not steps:
        return None
    return {"name": name, "ingredients": ingredients, "steps": steps, "complete": False}


def _compact_partial_preview(value: Mapping[str, Any]) -> dict[str, Any] | None:
    """Bound previews from injected models to the public preview contract."""

    if not isinstance(value, Mapping):
        return None
    name = _safe_text(value.get("name") or value.get("title"), limit=120)
    ingredients: list[dict[str, Any]] = []
    raw_ingredients = value.get("ingredients") if isinstance(value.get("ingredients"), list) else []
    for item in raw_ingredients[:MAX_DRAFT_LIST_ITEMS]:
        if not isinstance(item, Mapping):
            continue
        item_name = _safe_text(item.get("name"), limit=120)
        if not item_name:
            continue
        amount = item.get("amount")
        if isinstance(amount, bool) or not isinstance(amount, (str, int, float)):
            amount = ""
        elif isinstance(amount, str):
            amount = amount[:80]
        ingredients.append({"name": item_name, "amount": amount, "unit": _safe_text(item.get("unit"), limit=40)})
    steps: list[dict[str, Any]] = []
    raw_steps = value.get("steps") if isinstance(value.get("steps"), list) else []
    for item in raw_steps[:MAX_DRAFT_LIST_ITEMS]:
        if not isinstance(item, Mapping):
            continue
        instruction = _safe_text(item.get("instruction"), limit=MAX_DRAFT_TEXT_LENGTH)
        if not instruction:
            continue
        duration = item.get("duration_seconds")
        if not isinstance(duration, (int, float)) or isinstance(duration, bool) or not math.isfinite(float(duration)) or duration <= 0:
            duration = None
        steps.append({
            "instruction": instruction,
            "duration_seconds": duration,
            "heat_level": _safe_text(item.get("heat_level"), limit=40) if item.get("heat_level") is not None else None,
        })
    if not name and not ingredients and not steps:
        return None
    return {"name": name, "ingredients": ingredients, "steps": steps, "complete": False}


def _parse_json_content(content: str) -> dict[str, Any]:
    text = str(content or "").strip()
    fence = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.IGNORECASE | re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        if start < 0:
            raise
        value, end = json.JSONDecoder().raw_decode(text[start:])
        trailing = text[start + end :].strip("`。，, \r\n")
        if trailing and not trailing.startswith(("谢谢", "以上")):
            raise json.JSONDecodeError("trailing", text, start + end)
    if not isinstance(value, dict):
        raise ValueError("JSON 根节点必须是对象")
    return value


def _model_result_to_dict(result: Any) -> dict[str, Any]:
    if isinstance(result, Mapping):
        return deepcopy(dict(result))
    if isinstance(result, str):
        try:
            return _parse_json_content(result)
        except (ValueError, json.JSONDecodeError) as exc:
            raise VideoImportError("视频分析结果无效，请重试或上传视频文件。", status_code=502) from exc
    raise VideoImportError("视频分析结果无效，请重试或上传视频文件。", status_code=502)


def _call_injected_model(
    model: Any,
    data_url: str,
    prompt: str,
    *,
    partial_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    if model is None:
        raise VideoImportError("未配置视频分析服务，请直接上传视频或配置千问 Key。", status_code=503)
    for method_name in ("analyze_video", "analyze", "infer"):
        method = getattr(model, method_name, None)
        if callable(method):
            try:
                if partial_callback is not None:
                    try:
                        return _model_result_to_dict(method(data_url, prompt, partial_callback=partial_callback))
                    except TypeError:
                        pass
                return _model_result_to_dict(method(data_url, prompt))
            except TypeError:
                # Accommodate tiny test doubles that expose keyword-only names.
                for kwargs in (
                    {"video_data_url": data_url, "prompt": prompt},
                    {"video": data_url, "instruction": prompt},
                ):
                    if partial_callback is not None:
                        kwargs["partial_callback"] = partial_callback
                    try:
                        return _model_result_to_dict(method(**kwargs))
                    except TypeError:
                        if partial_callback is not None:
                            kwargs.pop("partial_callback", None)
                            try:
                                return _model_result_to_dict(method(**kwargs))
                            except TypeError:
                                continue
                        continue
            except VideoImportError:
                raise
    if callable(model):
        try:
            return _model_result_to_dict(model(data_url, prompt))
        except TypeError:
            return _model_result_to_dict(model(video_data_url=data_url, prompt=prompt))
    raise VideoImportError("视频分析模型接口不可用，请稍后重试。", status_code=503)


def _call_injected_text_model(
    model: Any,
    evidence_text: str,
    prompt: str,
    *,
    partial_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Call a text organizer without falling back to another video upload."""

    if model is None:
        raise VideoImportError("未配置视频分析服务，请直接上传视频或配置千问 Key。", status_code=503)
    for method_name in ("organize_text", "analyze_text", "infer_text", "organize", "analyze", "infer"):
        method = getattr(model, method_name, None)
        if not callable(method):
            continue
        try:
            if partial_callback is not None:
                try:
                    return _model_result_to_dict(method(evidence_text, prompt, partial_callback=partial_callback))
                except TypeError:
                    pass
            return _model_result_to_dict(method(evidence_text, prompt))
        except TypeError:
            for kwargs in (
                {"evidence_text": evidence_text, "prompt": prompt},
                {"text": evidence_text, "instruction": prompt},
            ):
                if partial_callback is not None:
                    kwargs["partial_callback"] = partial_callback
                try:
                    return _model_result_to_dict(method(**kwargs))
                except TypeError:
                    if partial_callback is not None:
                        kwargs.pop("partial_callback", None)
                        try:
                            return _model_result_to_dict(method(**kwargs))
                        except TypeError:
                            continue
                    continue
        except VideoImportError:
            raise
    if callable(model):
        try:
            return _model_result_to_dict(model(evidence_text, prompt))
        except TypeError:
            try:
                return _model_result_to_dict(model(text=evidence_text, prompt=prompt))
            except TypeError:
                pass
    raise VideoImportError("视频文本整理模型接口不可用，请稍后重试。", status_code=503)


def _recipe_completion_prompt(payload: Mapping[str, Any], *, include_recipe: bool = True) -> str:
    """Build a compact text-only prompt for filling a review draft."""

    recipe = payload.get("recipe") if isinstance(payload.get("recipe"), Mapping) else {}
    missing = payload.get("missing_fields") if isinstance(payload.get("missing_fields"), list) else []
    quality = payload.get("quality_issues") if isinstance(payload.get("quality_issues"), list) else []
    title = _safe_text(payload.get("title"), limit=160)
    recipe_json = json.dumps(recipe, ensure_ascii=False, separators=(",", ":"), default=str)[:24_000]
    missing_json = json.dumps([_safe_text(item, limit=160) for item in missing[:64]], ensure_ascii=False, separators=(",", ":"))
    quality_json = json.dumps([_safe_text(item, limit=240) for item in quality[:32]], ensure_ascii=False, separators=(",", ":"))
    prompt = (
        "你是菜谱草稿补全助手。只补全当前菜谱缺失字段，保留已有值，不要删除或改写用户已有内容。"
        "视频已有的‘适量、少量、按口味、一圈’是有效用量，保留这些表述；只为真正空缺的信息给建议。"
        "例外：主要肉类用量只有适量等模糊表述时，按当前人数估算具体克数或个数，标为 AI 建议；不要修改视频已有明确用量。"
        "无法从当前草稿确定时，按常见家常做法给出安全、可执行的 AI 建议；这些字段必须列入 ai_completion_fields，"
        "不要声称它们来自视频，也不要输出视频证据。只输出待补部分的 JSON，省略不需修改的项与字段。"
        '食材用 ingredients:[{name:原名称,amount:建议用量,unit:建议单位}]；'
        '步骤用 steps:[{instruction:原操作说明,duration_seconds:建议秒数,heat_level:建议火力}]，新增必要步骤按烹饪顺序排列；'
        '其余缺失字段直接按字段名给值，ai_completion_fields列出原草稿中对应路径。'
        f"标题：{title}。待补字段：{missing_json}。质量提醒（仅供参考，不代表阻止确认）：{quality_json}。"
    )
    return f"{prompt}当前菜谱：{recipe_json}" if include_recipe else prompt


def _call_injected_completion_model(
    model: Any,
    recipe: Mapping[str, Any],
    missing_fields: list[str],
    quality_issues: list[str],
    *,
    title: str,
    partial_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Call a model's structured completion hook, then its text hook."""

    if model is None:
        raise VideoImportError("未配置文本补全服务，请稍后重试。", status_code=503)
    prompt_payload = {
        "recipe": recipe,
        "missing_fields": missing_fields,
        "quality_issues": quality_issues,
        "title": title,
    }
    prompt = _recipe_completion_prompt(prompt_payload)
    method = getattr(model, "complete_recipe", None)
    if callable(method):
        attempts = [
            ((recipe, missing_fields, quality_issues), {"title": title, "partial_callback": partial_callback}),
            ((recipe, missing_fields, quality_issues), {"title": title}),
            ((), {"recipe": recipe, "missing_fields": missing_fields, "quality_issues": quality_issues, "title": title, "partial_callback": partial_callback}),
            ((json.dumps({"recipe": recipe, "missing_fields": missing_fields, "quality_issues": quality_issues}, ensure_ascii=False, separators=(",", ":")), prompt), {"partial_callback": partial_callback}),
            ((json.dumps({"recipe": recipe, "missing_fields": missing_fields, "quality_issues": quality_issues}, ensure_ascii=False, separators=(",", ":")), prompt), {}),
        ]
        for args, kwargs in attempts:
            if kwargs.get("partial_callback") is None:
                kwargs.pop("partial_callback", None)
            try:
                return _model_result_to_dict(method(*args, **kwargs))
            except TypeError:
                continue
        # A completion hook exists but exposes an unsupported signature.  Do
        # not silently fall back to the video method; text-only is still safe.
    text_payload = json.dumps(
        {"recipe": recipe, "missing_fields": missing_fields[:64], "quality_issues": quality_issues[:32], "title": title},
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )[:24_000]
    return _call_injected_text_model(
        model,
        text_payload,
        _recipe_completion_prompt(prompt_payload, include_recipe=False),
        partial_callback=partial_callback,
    )


def _recipe_from_model(result: Mapping[str, Any]) -> dict[str, Any] | None:
    for key in ("recipe", "draft", "recipe_draft", "final_recipe"):
        nested = result.get(key)
        if isinstance(nested, Mapping):
            candidate = deepcopy(dict(nested))
            if "name" not in candidate and candidate.get("title"):
                candidate["name"] = candidate["title"]
            return candidate
    if isinstance(result.get("name"), str) and isinstance(result.get("ingredients"), list) and isinstance(result.get("steps"), list):
        return deepcopy(dict(result))
    if isinstance(result.get("title"), str) and isinstance(result.get("ingredients"), list) and isinstance(result.get("steps"), list):
        candidate = deepcopy(dict(result))
        candidate["name"] = candidate["title"]
        return candidate
    return None


def _evidence_to_recipe(evidence: Mapping[str, Any], source: VideoSource) -> dict[str, Any]:
    recipe = _recipe_from_model(evidence)
    if recipe is not None:
        return recipe
    title = evidence.get("title") or source.title
    ingredients = evidence.get("ingredients") if isinstance(evidence.get("ingredients"), list) else []
    steps = evidence.get("steps") if isinstance(evidence.get("steps"), list) else []
    return {
        "name": _safe_text(title, limit=120) or "视频菜谱",
        "servings": evidence.get("servings") or 1,
        "estimated_time_minutes": evidence.get("estimated_time_minutes") or evidence.get("estimated_minutes"),
        "difficulty": evidence.get("difficulty") or "简单",
        "ingredients": deepcopy(ingredients),
        "equipment": deepcopy(evidence.get("equipment") if isinstance(evidence.get("equipment"), list) else []),
        "notes": deepcopy(evidence.get("notes") if isinstance(evidence.get("notes"), list) else []),
        "steps": deepcopy(steps),
        "is_tutorial": evidence.get("is_tutorial", True),
        "dish_count": evidence.get("dish_count", 1),
        "quality_issues": deepcopy(evidence.get("quality_issues") if isinstance(evidence.get("quality_issues"), list) else []),
    }


def _normalizer() -> Any:
    skill_root = Path(__file__).resolve().parents[1] / "skills" / "kitchen_assistant"
    if str(skill_root) not in sys.path:
        sys.path.insert(0, str(skill_root))
    from kitchen.recipe_normalizer import RecipeNormalizer

    return RecipeNormalizer()


def _sanitize_recipe_input(raw: Mapping[str, Any]) -> dict[str, Any]:
    name = raw.get("name") or raw.get("title")
    payload: dict[str, Any] = {
        "name": _safe_text(name, limit=120),
        "servings": raw.get("servings") if raw.get("servings") is not None else 1,
        "estimated_time_minutes": raw.get("estimated_time_minutes") or raw.get("estimated_minutes"),
        "difficulty": _safe_text(raw.get("difficulty") or "简单", limit=20),
        "equipment": [
            _safe_text(item, limit=80)
            for item in (raw.get("equipment") if isinstance(raw.get("equipment"), list) else [])[:MAX_DRAFT_LIST_ITEMS]
            if _safe_text(item, limit=80)
        ],
        "notes": [
            _safe_text(item, limit=240)
            for item in (raw.get("notes") or raw.get("safety_notes") if isinstance(raw.get("notes") or raw.get("safety_notes"), list) else [])[:MAX_DRAFT_LIST_ITEMS]
            if _safe_text(item, limit=240)
        ],
        "ingredients": [],
        "steps": [],
    }
    ingredients = raw.get("ingredients") if isinstance(raw.get("ingredients"), list) else []
    for item in ingredients[:MAX_DRAFT_LIST_ITEMS]:
        if isinstance(item, str):
            payload["ingredients"].append({"name": _safe_text(item, limit=120), "amount": "", "unit": "", "optional": False})
            continue
        if not isinstance(item, Mapping):
            continue
        payload["ingredients"].append({
            "name": _safe_text(item.get("name"), limit=120),
            "amount": item.get("amount") if item.get("amount") is not None else "",
            "unit": _safe_text(item.get("unit"), limit=40),
            "optional": bool(item.get("optional", False)),
        })
    steps = raw.get("steps") if isinstance(raw.get("steps"), list) else []
    for item in steps[:MAX_DRAFT_LIST_ITEMS]:
        if isinstance(item, str):
            payload["steps"].append({"instruction": _safe_text(item, limit=MAX_DRAFT_TEXT_LENGTH), "duration_seconds": None, "heat_level": None, "safety_note": None})
            continue
        if not isinstance(item, Mapping):
            continue
        payload["steps"].append({key: deepcopy(item.get(key)) for key in _STEP_FIELDS if key in item})
        payload["steps"][-1].setdefault("instruction", "")
        payload["steps"][-1].setdefault("duration_seconds", None)
        payload["steps"][-1].setdefault("heat_level", None)
        payload["steps"][-1].setdefault("safety_note", None)
    # Preserve explicit quality fields only in the transient draft; they are
    # consumed by the gate and never reach the runtime recipe.
    for key in ("is_tutorial", "dish_count", "quality_issues", "missing_critical_fields", "confidence"):
        if key in raw:
            payload[key] = deepcopy(raw[key])
    return payload


def _iter_field_values(recipe: Mapping[str, Any]) -> Iterable[tuple[str, Any]]:
    for key in ("name", "servings", "estimated_time_minutes", "difficulty"):
        yield key, recipe.get(key)
    for index, item in enumerate(recipe.get("ingredients") if isinstance(recipe.get("ingredients"), list) else []):
        if isinstance(item, Mapping):
            for key in ("name", "amount", "unit", "optional"):
                yield f"ingredients[{index}].{key}", item.get(key)
    for index, item in enumerate(recipe.get("steps") if isinstance(recipe.get("steps"), list) else []):
        if isinstance(item, Mapping):
            for key in ("instruction", "duration_seconds", "heat_level", "safety_note"):
                yield f"steps[{index}].{key}", item.get(key)


_COMPLETION_TIMED_ACTIONS = (
    "腌制", "浸泡", "泡发", "静置", "醒发", "预热", "烧开", "沸腾", "煮", "炖", "焖",
    "煎", "烤", "蒸", "炸", "熬", "焯", "炒", "煸", "收汁", "微波", "电饭煲",
)


def _is_missing_recipe_value(value: Any, *, text: bool = False) -> bool:
    if value is None or value == "":
        return True
    if text:
        return not str(value).strip()
    return False


def _is_missing_recipe_unit(value: Any) -> bool:
    return _is_missing_recipe_value(value, text=True)


def _missing_completion_fields(recipe: Mapping[str, Any]) -> list[str]:
    """Return bounded paths that a text model may usefully complete."""

    missing: list[str] = []
    if _is_missing_recipe_value(recipe.get("name"), text=True):
        missing.append("name")
    ingredients = recipe.get("ingredients") if isinstance(recipe.get("ingredients"), list) else []
    if not ingredients:
        missing.append("ingredients")
    for index, item in enumerate(ingredients[:MAX_DRAFT_LIST_ITEMS]):
        if not isinstance(item, Mapping):
            missing.extend((f"ingredients[{index}].name", f"ingredients[{index}].amount", f"ingredients[{index}].unit"))
            continue
        if _is_missing_recipe_value(item.get("name"), text=True):
            missing.append(f"ingredients[{index}].name")
        if _is_missing_recipe_value(item.get("amount")):
            missing.append(f"ingredients[{index}].amount")
        if _is_missing_recipe_unit(item.get("unit")) and not _contains_vague_amount(item.get("amount")):
            missing.append(f"ingredients[{index}].unit")
    steps = recipe.get("steps") if isinstance(recipe.get("steps"), list) else []
    if not steps:
        missing.append("steps")
    for index, item in enumerate(steps[:MAX_DRAFT_LIST_ITEMS]):
        if not isinstance(item, Mapping):
            missing.append(f"steps[{index}].instruction")
            continue
        instruction = item.get("instruction")
        if _is_missing_recipe_value(instruction, text=True):
            missing.append(f"steps[{index}].instruction")
        elif item.get("duration_seconds") in (None, "") and any(action in str(instruction) for action in _COMPLETION_TIMED_ACTIONS):
            missing.append(f"steps[{index}].duration_seconds")
    # Preserve insertion order while preventing a provider from expanding the
    # completion request into an unbounded list of repeated paths.
    return list(dict.fromkeys(missing))[:MAX_DRAFT_LIST_ITEMS * 2]


def _draft_quality_issues(recipe: Mapping[str, Any]) -> list[str]:
    metadata = recipe.get("import_metadata") if isinstance(recipe.get("import_metadata"), Mapping) else {}
    values: list[Any] = []
    for source in (
        recipe.get("quality_issues"),
        recipe.get("missing_critical_fields"),
        metadata.get("quality_issues"),
        metadata.get("missing_critical_fields"),
    ):
        if isinstance(source, list):
            values.extend(source[:32])
    return [_safe_text(item, limit=240) for item in values if _safe_text(item, limit=240)]


def _completion_fields_for_draft(recipe: Mapping[str, Any]) -> list[str]:
    missing = _missing_completion_fields(recipe)
    for index, item in enumerate(recipe.get("ingredients", [])[:MAX_DRAFT_LIST_ITEMS]):
        if isinstance(item, Mapping) and _main_meat_needs_quantity(item):
            missing.append(f"ingredients[{index}].amount")
            missing.append(f"ingredients[{index}].unit")
    # A quality warning about an omitted key step is useful context for the
    # text model even when the current list is non-empty. It remains a review
    # notice if the model cannot supply an additional executable step.
    issues = _draft_quality_issues(recipe)
    if issues and any(
        any(word in issue for word in ("步骤", "做法")) and any(marker in issue for marker in ("缺", "少", "遗漏", "不完整"))
        for issue in issues
    ):
        missing.append("steps")
    return list(dict.fromkeys(missing))[:MAX_DRAFT_LIST_ITEMS * 2]


def _main_meat_needs_quantity(item: Mapping[str, Any]) -> bool:
    name = str(item.get("name") or "")
    meat_names = ("鸡腿", "鸡翅", "鸡胸", "鸡肉", "鸭肉", "鸭腿", "牛肉", "猪肉", "羊肉", "肉馅", "瘦肉", "排骨", "肥牛", "里脊", "五花肉", "鱼肉", "鱼片", "鲈鱼", "鳕鱼", "三文鱼", "龙利鱼", "鲫鱼", "虾仁", "基围虾")
    return any(meat in name for meat in meat_names) and not re.search(r"\d|[一二两三四五六七八九十半]", str(item.get("amount") or ""))


def _draft_fingerprint(draft: Mapping[str, Any] | None) -> str:
    try:
        payload = json.dumps(draft or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        payload = repr(draft)
    return hashlib.sha256(payload.encode("utf-8", errors="replace")).hexdigest()


def _completion_candidate(result: Mapping[str, Any], base: Mapping[str, Any] | None = None) -> dict[str, Any]:
    candidate = _recipe_from_model(result)
    if candidate is not None:
        return candidate
    for key in ("completion", "completed", "fields"):
        nested = result.get(key)
        if isinstance(nested, Mapping):
            return _completion_candidate(nested, base)
    candidate = deepcopy(dict(result))
    for path, value in result.items():
        match = re.fullmatch(r"(ingredients|steps)(?:\[(\d+)\]|\.(\d+))\.([a-z_]+)", str(path))
        if not match:
            continue
        group, index, field = match[1], int(match[2] or match[3]), match[4]
        if index >= MAX_DRAFT_LIST_ITEMS:
            continue
        rows = candidate.setdefault(group, [])
        if not isinstance(rows, list):
            continue
        while len(rows) <= index:
            rows.append({})
        if not isinstance(rows[index], dict):
            rows[index] = {}
        base_rows = base.get(group, []) if isinstance(base, Mapping) else []
        identity = "name" if group == "ingredients" else "instruction"
        if index < len(base_rows) and isinstance(base_rows[index], Mapping):
            rows[index].setdefault(identity, base_rows[index].get(identity))
        rows[index][field] = deepcopy(value)
    return candidate


def _merge_completion_draft(
    base: Mapping[str, Any],
    candidate: Mapping[str, Any],
    *,
    allow_extra_steps: bool = False,
) -> tuple[dict[str, Any], list[str]]:
    """Merge AI values only into blank fields of the captured review draft."""

    working = _sanitize_recipe_input(base)
    changed: list[str] = []

    def missing(value: Any, *, text: bool = False) -> bool:
        return _is_missing_recipe_value(value, text=text)

    def set_if_missing(container: dict[str, Any], key: str, path: str, value: Any, *, text: bool = False) -> None:
        if missing(container.get(key), text=text) and not missing(value, text=text):
            container[key] = deepcopy(value)
            changed.append(path)

    candidate_safe = _sanitize_recipe_input(candidate)
    for key, text in (("name", True), ("estimated_time_minutes", False), ("difficulty", True)):
        set_if_missing(working, key, key, candidate_safe.get(key), text=text)

    base_ingredients = working.get("ingredients") if isinstance(working.get("ingredients"), list) else []
    candidate_ingredients = candidate_safe.get("ingredients") if isinstance(candidate_safe.get("ingredients"), list) else []
    if not base_ingredients and candidate_ingredients:
        working["ingredients"] = deepcopy(candidate_ingredients[:MAX_DRAFT_LIST_ITEMS])
        for index, item in enumerate(working["ingredients"]):
            if isinstance(item, Mapping):
                for key in ("name", "amount", "unit"):
                    if not missing(item.get(key), text=key != "amount"):
                        changed.append(f"ingredients[{index}].{key}")
    else:
        used_ingredients: set[int] = set()
        for index, item in enumerate(base_ingredients[:MAX_DRAFT_LIST_ITEMS]):
            if not isinstance(item, dict):
                continue
            preferred_quantity = _main_meat_needs_quantity(item)
            if not preferred_quantity and not any(missing(item.get(key), text=key != "amount") for key in ("name", "amount")) and (not _is_missing_recipe_unit(item.get("unit")) or _contains_vague_amount(item.get("amount"))):
                continue
            candidate_index = index if index < len(candidate_ingredients) else None
            item_name = str(item.get("name") or "").strip()
            if item_name:
                by_name = next(
                    (
                        candidate_index_value
                        for candidate_index_value, candidate_item in enumerate(candidate_ingredients)
                        if candidate_index_value not in used_ingredients
                        and isinstance(candidate_item, Mapping)
                        and str(candidate_item.get("name") or "").strip() == item_name
                    ),
                    None,
                )
                if by_name is not None:
                    candidate_index = by_name
                else:
                    continue
            if candidate_index is None or candidate_index in used_ingredients or not isinstance(candidate_ingredients[candidate_index], Mapping):
                continue
            used_ingredients.add(candidate_index)
            suggestion = candidate_ingredients[candidate_index]
            amount_missing = missing(item.get("amount")) or preferred_quantity
            if preferred_quantity and not _main_meat_needs_quantity(suggestion) and not missing(suggestion.get("amount")):
                item["amount"] = deepcopy(suggestion["amount"])
                changed.append(f"ingredients[{index}].amount")
            for key in ("name", "amount", "unit"):
                set_if_missing(item, key, f"ingredients[{index}].{key}", suggestion.get(key), text=key != "amount")
            suggested_unit = suggestion.get("unit")
            if (amount_missing or _is_missing_recipe_unit(item.get("unit"))) and not _is_missing_recipe_unit(suggested_unit) and suggested_unit != item.get("unit"):
                item["unit"] = deepcopy(suggested_unit)
                changed.append(f"ingredients[{index}].unit")

    base_steps = working.get("steps") if isinstance(working.get("steps"), list) else []
    candidate_steps = candidate_safe.get("steps") if isinstance(candidate_safe.get("steps"), list) else []
    if not base_steps and candidate_steps:
        working["steps"] = deepcopy(candidate_steps[:MAX_DRAFT_LIST_ITEMS])
        for index, item in enumerate(working["steps"]):
            if isinstance(item, Mapping):
                for key in ("instruction", "duration_seconds", "heat_level", "safety_note"):
                    if not missing(item.get(key), text=key in {"instruction", "heat_level", "safety_note"}):
                        changed.append(f"steps[{index}].{key}")
    else:
        used_steps: set[int] = set()
        candidate_to_step: dict[int, dict[str, Any]] = {}
        original_steps = list(base_steps)
        for index, item in enumerate(base_steps[:MAX_DRAFT_LIST_ITEMS]):
            if not isinstance(item, dict):
                continue
            candidate_index = index if index < len(candidate_steps) else None
            instruction = str(item.get("instruction") or "").strip()
            if instruction:
                instruction_compact = re.sub(r"\s+", "", instruction)
                by_instruction = next(
                    (
                        candidate_index_value
                        for candidate_index_value, candidate_item in enumerate(candidate_steps)
                        if candidate_index_value not in used_steps
                        and isinstance(candidate_item, Mapping)
                        and (
                            instruction_compact in re.sub(r"\s+", "", str(candidate_item.get("instruction") or ""))
                            or re.sub(r"\s+", "", str(candidate_item.get("instruction") or "")) in instruction_compact
                        )
                    ),
                    None,
                )
                if by_instruction is not None:
                    candidate_index = by_instruction
                else:
                    continue
            if candidate_index is None or candidate_index in used_steps or not isinstance(candidate_steps[candidate_index], Mapping):
                continue
            used_steps.add(candidate_index)
            candidate_to_step[candidate_index] = item
            suggestion = candidate_steps[candidate_index]
            set_if_missing(item, "instruction", f"steps[{index}].instruction", suggestion.get("instruction"), text=True)
            set_if_missing(item, "duration_seconds", f"steps[{index}].duration_seconds", suggestion.get("duration_seconds"))
            set_if_missing(item, "heat_level", f"steps[{index}].heat_level", suggestion.get("heat_level"), text=True)
            set_if_missing(item, "safety_note", f"steps[{index}].safety_note", suggestion.get("safety_note"), text=True)
        if allow_extra_steps and len(candidate_steps) > len(used_steps):
            for candidate_index, suggestion in enumerate(candidate_steps[:MAX_DRAFT_LIST_ITEMS]):
                if candidate_index in used_steps or not isinstance(suggestion, Mapping):
                    continue
                instruction = suggestion.get("instruction")
                if not isinstance(instruction, str) or not instruction.strip():
                    continue
                next_step = next((candidate_to_step[key] for key in sorted(candidate_to_step) if key > candidate_index), None)
                insert_at = next((i for i, item in enumerate(working["steps"]) if item is next_step), len(working["steps"]))
                added = deepcopy(dict(suggestion))
                working["steps"].insert(insert_at, added)
                candidate_to_step[candidate_index] = added
                used_steps.add(candidate_index)
            remap = {old: next(i for i, item in enumerate(working["steps"]) if item is step) for old, step in enumerate(original_steps)}
            changed = [re.sub(r"^steps\[(\d+)\]", lambda match: f"steps[{remap[int(match[1])]}]", path) if path.startswith("steps[") else path for path in changed]
            for index, step in enumerate(working["steps"]):
                if any(step is old for old in original_steps):
                    continue
                changed.extend(f"steps[{index}].{key}" for key in ("instruction", "duration_seconds", "heat_level", "safety_note") if not missing(step.get(key), text=key != "duration_seconds"))

    return working, list(dict.fromkeys(changed))[:MAX_DRAFT_LIST_ITEMS * 4]


def _set_ai_completion_origins(draft: dict[str, Any], paths: Iterable[str]) -> None:
    metadata = draft.get("import_metadata") if isinstance(draft.get("import_metadata"), dict) else None
    if metadata is None:
        return
    origins = metadata.setdefault("field_origins", {})
    if not isinstance(origins, dict):
        origins = {}
        metadata["field_origins"] = origins
    completed = metadata.setdefault("ai_completed_fields", [])
    if not isinstance(completed, list):
        completed = []
        metadata["ai_completed_fields"] = completed
    for path in paths:
        clean = _safe_text(path, limit=160)
        if not clean:
            continue
        record = {"origin": "ai_completion", "status": "ai_completion", "evidence": []}
        origins[clean] = record
        dotted = re.sub(r"\[(\d+)\]", r".\1", clean)
        origins[dotted] = deepcopy(record)
        if clean not in completed:
            completed.append(clean)
    metadata["ai_completed"] = bool(completed)
    metadata["ai_completions"] = list(dict.fromkeys([*metadata.get("ai_completions", []), *completed]))
    metadata["field_status"] = deepcopy(origins)


def _evidence_items(result: Mapping[str, Any], *, segment_start: float, segment_end: float) -> list[dict[str, Any]]:
    raw_items = result.get("evidence") or result.get("observations") or result.get("field_evidence")
    if not isinstance(raw_items, list):
        raw_items = []
    items: list[dict[str, Any]] = []
    for item in raw_items[:MAX_DRAFT_LIST_ITEMS * 2]:
        if not isinstance(item, Mapping):
            continue
        start = _as_number(item.get("start_seconds") or item.get("start"))
        end = _as_number(item.get("end_seconds") or item.get("end"))
        if start is None:
            start = segment_start
        if end is None:
            end = min(segment_end, max(start, segment_start + 5))
        # Segment-relative timestamps are shifted into the original video.
        if start < segment_start:
            start += segment_start
        if end < segment_start:
            end += segment_start
        items.append({
            "field": _safe_text(item.get("field") or item.get("path") or "", limit=160),
            "value": deepcopy(item.get("value")),
            "start_seconds": round(max(0.0, start), 3),
            "end_seconds": round(max(start, end), 3),
            "confidence": _safe_text(item.get("confidence") or result.get("confidence") or "unknown", limit=20),
            "origin": "video_fact",
        })
    if not items:
        items.append({
            "field": "video",
            "value": None,
            "start_seconds": round(max(0.0, segment_start), 3),
            "end_seconds": round(max(segment_start, segment_end), 3),
            "confidence": _safe_text(result.get("confidence") or "unknown", limit=20),
            "origin": "video_fact",
        })
    return items


def _contains_vague_amount(value: Any) -> bool:
    text = str(value or "")
    return any(marker in text for marker in ("适量", "少量", "按口味", "一圈"))


_DURATION_TEXT_RE = re.compile(r"(?P<amount>\d+(?:\.\d+)?|[一二两三四五六七八九十百]+)\s*(?P<unit>小时|分钟|分|秒)")


def _duration_from_instruction(instruction: Any) -> int | None:
    """Read an explicit duration phrase, leaving absent timing unknown."""

    match = _DURATION_TEXT_RE.search(str(instruction or ""))
    if match is None:
        return None
    amount_text = match.group("amount")
    try:
        if amount_text.isdigit() or "." in amount_text:
            amount = float(amount_text)
        else:
            digits = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
            if amount_text == "十":
                amount = 10
            elif "十" in amount_text:
                left, _, right = amount_text.partition("十")
                amount = (digits.get(left, 1) * 10 if left else 10) + (digits.get(right, 0) if right else 0)
            elif "百" in amount_text:
                left, _, right = amount_text.partition("百")
                amount = digits.get(left, 1) * 100 + (digits.get(right, 0) if right else 0)
            else:
                amount = digits.get(amount_text, 0)
        multiplier = {"小时": 3600, "分钟": 60, "分": 60, "秒": 1}[match.group("unit")]
        seconds = int(amount * multiplier)
    except (KeyError, TypeError, ValueError):
        return None
    return seconds if seconds > 0 else None


def _strip_inferred_marinade_duration(instruction: Any) -> str:
    """Remove a timer the legacy normalizer invented for an untimed step."""

    text = str(instruction or "")
    text = re.sub(
        r"((?:继续)?腌制)\s*(?:\d+(?:\.\d+)?|[一二两三四五六七八九十百]+)\s*(?:小时|分钟|分|秒)",
        r"\1",
        text,
    )
    return text.strip(" ，,；;。")


def _duration_evidence(metadata: Mapping[str, Any], index: int) -> int | None:
    evidence = metadata.get("evidence") if isinstance(metadata.get("evidence"), list) else []
    expected = {f"steps[{index}].duration_seconds", f"steps.{index}.duration_seconds"}
    for item in evidence:
        if not isinstance(item, Mapping):
            continue
        field = str(item.get("field") or item.get("path") or "")
        if field not in expected:
            continue
        value = item.get("value")
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)) and value > 0:
            return int(value)
        parsed = _duration_from_instruction(value)
        if parsed is not None:
            return parsed
    return None


def _ai_completed_duration(metadata: Mapping[str, Any], index: int) -> bool:
    paths = metadata.get("ai_completed_fields") if isinstance(metadata.get("ai_completed_fields"), list) else []
    expected = {f"steps[{index}].duration_seconds", f"steps.{index}.duration_seconds"}
    return any(str(path) in expected for path in paths)


def _step_user_edited(metadata: Mapping[str, Any]) -> bool:
    edits = metadata.get("user_edits") if isinstance(metadata.get("user_edits"), list) else []
    return any(str(item.get("field") or item.get("path") or "").startswith("steps") for item in edits if isinstance(item, Mapping))


def _matching_raw_step(raw_steps: list[Any], normalized_instruction: str, index: int) -> Mapping[str, Any] | None:
    if not raw_steps:
        return None
    normalized_text = re.sub(r"\s+", "", normalized_instruction)
    indexed = raw_steps[min(index, len(raw_steps) - 1)]
    if isinstance(indexed, Mapping):
        indexed_text = re.sub(r"\s+", "", str(indexed.get("instruction") or ""))
        if not normalized_text or not indexed_text or normalized_text in indexed_text or indexed_text in normalized_text:
            return indexed
    for candidate in raw_steps:
        if not isinstance(candidate, Mapping):
            continue
        candidate_text = re.sub(r"\s+", "", str(candidate.get("instruction") or ""))
        if candidate_text and (candidate_text in normalized_text or normalized_text in candidate_text):
            return candidate
    return indexed if isinstance(indexed, Mapping) else None


def _apply_import_timing_policy(
    raw_recipe: Mapping[str, Any],
    target_recipe: dict[str, Any],
    metadata: Mapping[str, Any],
) -> None:
    """Keep only stated/evidenced/user-entered timers in imported drafts."""

    raw_steps = raw_recipe.get("steps") if isinstance(raw_recipe.get("steps"), list) else []
    target_steps = target_recipe.get("steps") if isinstance(target_recipe.get("steps"), list) else []
    user_edited_steps = _step_user_edited(metadata)
    for index, target_step in enumerate(target_steps):
        if not isinstance(target_step, dict):
            continue
        target_instruction = str(target_step.get("instruction") or "")
        raw_step = _matching_raw_step(raw_steps, target_instruction, index)
        raw_instruction = str(raw_step.get("instruction") or "") if raw_step is not None else ""
        raw_explicit = _duration_from_instruction(raw_instruction)
        explicit_seconds = _duration_from_instruction(target_instruction) if raw_explicit is not None else None
        raw_index = next((i for i, step in enumerate(raw_steps) if step is raw_step), index)
        unchanged = target_instruction == raw_instruction
        normalized_timer = target_step.get("duration_seconds")
        can_keep_timer = unchanged or (isinstance(normalized_timer, (int, float)) and not isinstance(normalized_timer, bool) and normalized_timer > 0)
        evidence_seconds = _duration_evidence(metadata, raw_index) if can_keep_timer else None
        raw_duration = raw_step.get("duration_seconds") if raw_step is not None else None
        user_duration = (
            int(raw_duration)
            if can_keep_timer and user_edited_steps and isinstance(raw_duration, (int, float)) and not isinstance(raw_duration, bool)
            and math.isfinite(float(raw_duration)) and raw_duration > 0
            else None
        )
        if explicit_seconds is not None:
            target_step["duration_seconds"] = explicit_seconds
        elif evidence_seconds is not None:
            target_step["duration_seconds"] = evidence_seconds
        elif user_duration is not None:
            target_step["duration_seconds"] = user_duration
        elif _ai_completed_duration(metadata, raw_index):
            # A text-only completion is an explicit AI suggestion.  Preserve
            # it in the review draft while its provenance remains AI, rather
            # than silently presenting it as a video fact.
            if isinstance(normalized_timer, (int, float)) and not isinstance(normalized_timer, bool) and math.isfinite(float(normalized_timer)) and normalized_timer > 0:
                target_step["duration_seconds"] = normalized_timer
        else:
            target_step["duration_seconds"] = None
            # RecipeNormalizer adds “腌制10分钟” when the source only says
            # “腌制”. Remove that inferred phrase so the prose and field both
            # remain honest about the missing timing.
        if raw_explicit is None and "腌制" in raw_instruction:
            target_step["instruction"] = _strip_inferred_marinade_duration(target_step.get("instruction"))


def _ensure_import_field_origins(draft: Mapping[str, Any]) -> None:
    metadata = draft.get("import_metadata") if isinstance(draft.get("import_metadata"), dict) else None
    if metadata is None:
        return
    origins = metadata.setdefault("field_origins", {})
    if not isinstance(origins, dict):
        origins = {}
        metadata["field_origins"] = origins
    for path, _value in _iter_field_values(draft):
        if path not in origins:
            origins[path] = {"origin": "ai_completion", "status": "ai_completion", "evidence": []}
        dotted = re.sub(r"\[(\d+)\]", r".\1", path)
        if dotted not in origins:
            origins[dotted] = deepcopy(origins[path])
    metadata["field_status"] = deepcopy(origins)
    metadata["ai_completions"] = [path for path, value in origins.items() if isinstance(value, Mapping) and value.get("origin") == "ai_completion"]


def _validate_recipe_quality(raw: Mapping[str, Any], metadata: Mapping[str, Any]) -> None:
    if raw.get("is_tutorial") is False or metadata.get("is_tutorial") is False:
        raise VideoImportError("该视频不像单道菜教程，暂不能生成菜谱。", status_code=422)
    dish_count = raw.get("dish_count", metadata.get("dish_count"))
    if isinstance(dish_count, (int, float)) and not isinstance(dish_count, bool) and dish_count > 1:
        raise VideoImportError("该视频包含多道菜，暂不能确认成一道菜谱。", status_code=422)
    # Provider quality flags are retained as review notices. Confirmation is
    # gated by the current recipe shape and the hard tutorial/dish-count
    # checks above, so a stale warning cannot block a user-edited complete
    # draft forever.
    if not _safe_text(raw.get("name") or raw.get("title"), limit=120):
        raise VideoImportError("视频中没有可靠菜名，暂不能确认菜谱。", status_code=422)
    ingredients = raw.get("ingredients")
    steps = raw.get("steps")
    if not isinstance(ingredients, list) or not ingredients or not isinstance(steps, list) or not steps:
        raise VideoImportError("视频缺少完整食材或步骤，暂不能确认菜谱。", status_code=422)
    for item in ingredients:
        if not isinstance(item, Mapping) or not _safe_text(item.get("name"), limit=120):
            raise VideoImportError("视频缺少可靠食材信息，暂不能确认菜谱。", status_code=422)
        amount = item.get("amount")
        if _is_missing_recipe_value(amount, text=True):
            raise VideoImportError("部分食材用量无法从视频确认，请先补全后再确认。", status_code=422)
        if _is_missing_recipe_unit(item.get("unit")) and not _contains_vague_amount(amount):
            raise VideoImportError("部分食材单位无法从视频确认，请先补全后再确认。", status_code=422)
    for item in steps:
        if not isinstance(item, Mapping) or not _safe_text(item.get("instruction"), limit=MAX_DRAFT_TEXT_LENGTH):
            raise VideoImportError("视频缺少关键步骤说明，暂不能确认菜谱。", status_code=422)


def _strip_runtime_steps(recipe: dict[str, Any]) -> None:
    steps = recipe.get("steps")
    if not isinstance(steps, list):
        return
    clean_steps: list[dict[str, Any]] = []
    for index, step in enumerate(steps, start=1):
        if not isinstance(step, Mapping):
            continue
        cleaned = {key: deepcopy(step[key]) for key in _STEP_FIELDS if key in step}
        cleaned["step_number"] = index
        cleaned.setdefault("instruction", "")
        clean_steps.append(cleaned)
    recipe["steps"] = clean_steps


def _diff_values(before: Mapping[str, Any], after: Mapping[str, Any]) -> list[dict[str, Any]]:
    before_map = dict(_iter_field_values(before))
    after_map = dict(_iter_field_values(after))
    diffs: list[dict[str, Any]] = []
    for path in sorted(set(before_map) | set(after_map)):
        old = before_map.get(path)
        new = after_map.get(path)
        if old == new:
            continue
        diffs.append({"field": path, "before": deepcopy(old), "after": deepcopy(new), "origin": "rule_adjustment"})
    return diffs


def _build_import_metadata(
    source: VideoSource,
    *,
    media_info: MediaInfo,
    evidence: list[dict[str, Any]],
    draft: Mapping[str, Any],
    normalized: Mapping[str, Any],
    user_edits: list[dict[str, Any]] | None = None,
    diff: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    source_metadata = source.safe_metadata()
    source_metadata["duration_seconds"] = media_info.duration_seconds
    field_origins: dict[str, dict[str, Any]] = {}
    for field, _value in _iter_field_values(normalized):
        matches = [item for item in evidence if item.get("field") == field]
        if not matches:
            # Match a parent step/ingredient evidence path when the model used
            # a compact path such as ingredients[0].
            prefix = field.rsplit(".", 1)[0]
            matches = [item for item in evidence if item.get("field") == prefix]
        origin_record = {
            "origin": "video_fact" if matches else "ai_completion",
            "status": "fact" if matches else "ai_completion",
            "evidence": deepcopy(matches),
        }
        field_origins[field] = origin_record
        # The console uses JSON-pointer-like dotted paths while model evidence
        # commonly uses Python-style ``ingredients[0].amount``.
        dotted = re.sub(r"\[(\d+)\]", r".\1", field)
        if dotted != field:
            field_origins[dotted] = deepcopy(origin_record)
    for edit in user_edits or []:
        path = str(edit.get("field") or edit.get("path") or "")
        if path:
            prior = field_origins.get(path, {"evidence": []})
            field_origins[path] = {
                "origin": "user_edit",
                "status": "user_edit",
                "evidence": deepcopy(prior.get("evidence") or []),
            }
            dotted = re.sub(r"\[(\d+)\]", r".\1", path)
            if dotted != path:
                field_origins[dotted] = deepcopy(field_origins[path])
    adjustments = diff or []
    for change in adjustments:
        path = str(change.get("field") or "")
        if path and path not in {str(item.get("field") or "") for item in (user_edits or [])}:
            prior = field_origins.get(path, {"evidence": []})
            field_origins[path] = {
                "origin": "rule_adjustment",
                "status": "rule_adjustment",
                "evidence": deepcopy(prior.get("evidence") or []),
            }
            dotted = re.sub(r"\[(\d+)\]", r".\1", path)
            if dotted != path:
                field_origins[dotted] = deepcopy(field_origins[path])
    return {
        "confirmed": False,
        "source": source_metadata,
        "evidence": deepcopy(evidence),
        "field_origins": field_origins,
        "field_status": deepcopy(field_origins),
        "ai_completions": [path for path, value in field_origins.items() if value.get("origin") == "ai_completion"],
        "user_edits": deepcopy(user_edits or []),
        "rule_adjustments": deepcopy(adjustments),
        "standardization_diff": deepcopy(adjustments),
        "servings_origin": "video_fact" if draft.get("servings") else "ai_completion",
    }


class VideoImportService:
    """One-at-a-time asynchronous video import coordinator."""

    def __init__(
        self,
        manager: Any | None = None,
        *,
        store_dir: str | Path | None = None,
        adapter: Any | None = None,
        source_adapter: Any | None = None,
        platform_adapter: Any | None = None,
        model: Any | None = None,
        video_model: Any | None = None,
        llm_client: Any | None = None,
        text_model: Any | None = None,
        normalizer: Any | None = None,
        media_validator: Callable[..., Any] | None = None,
        downloader: Callable[..., Any] | None = None,
        temp_dir: str | Path | None = None,
        clock: Callable[[], float] = time.monotonic,
        task_timeout_seconds: float = MAX_TASK_SECONDS,
        max_workers: int = 1,
        **_: Any,
    ) -> None:
        self.manager = manager
        self.store = VideoRecipeStore(store_dir)
        temp_root = Path(temp_dir) if temp_dir is not None else self.store.store_dir / ".tmp"
        self.temp_root = temp_root
        cleanup_stale_temp_dirs(self.temp_root)
        self.adapter = adapter or source_adapter or platform_adapter or CompositeXiaohongshuAdapter()
        self.model = model or video_model
        if self.model is None and llm_client is not None:
            if any(callable(getattr(llm_client, name, None)) for name in ("analyze_video", "analyze", "infer")):
                self.model = llm_client
            elif callable(getattr(llm_client, "_get_client", None)):
                try:
                    self.model = QwenVideoModel(client=llm_client._get_client())
                except Exception:
                    self.model = QwenVideoModel()
            else:
                self.model = QwenVideoModel(client=llm_client)
        if self.model is None:
            # A manager may expose the existing Qwen client.  Avoid importing
            # or modifying the kitchen qwen_client module itself.
            candidate = next((getattr(manager, name, None) for name in ("llm_client", "qwen_client", "model") if getattr(manager, name, None) is not None), None)
            if candidate is not None and any(callable(getattr(candidate, name, None)) for name in ("analyze_video", "analyze", "infer")):
                self.model = candidate
            elif candidate is not None and callable(getattr(candidate, "_get_client", None)):
                try:
                    self.model = QwenVideoModel(client=candidate._get_client())
                except Exception:
                    self.model = QwenVideoModel()
            else:
                self.model = QwenVideoModel()
        self.normalizer = normalizer
        self.text_model = text_model
        self.media_validator = media_validator
        self.downloader = downloader
        self.clock = clock
        try:
            configured_timeout = float(task_timeout_seconds)
        except (TypeError, ValueError) as exc:
            raise VideoImportError("视频处理时限配置无效。", status_code=400) from exc
        if not math.isfinite(configured_timeout) or configured_timeout <= 0:
            raise VideoImportError("视频处理时限配置无效。", status_code=400)
        # Keep the production budget bounded at five minutes while allowing
        # injected tests to exercise watchdog behavior with a short deadline.
        self.task_timeout_seconds = min(MAX_TASK_SECONDS, max(0.01, configured_timeout))
        self._tasks: dict[str, _ImportTask] = {}
        self._dedupe: dict[str, str] = {}
        self._active_task_id: str | None = None
        self._lock = threading.RLock()
        self._threads: set[threading.Thread] = set()
        self.max_workers = max_workers

    def submit_link(self, share_text: str) -> dict[str, Any]:
        share_url = extract_share_url(share_text)
        key = f"link:{_source_key_from_url(share_url)}"
        with self._lock:
            existing = self._existing_for_key(key)
            if existing is not None:
                return self._snapshot(existing)
            self._ensure_slot()
            task = _ImportTask(
                task_id=uuid.uuid4().hex,
                source_kind="link",
                source_key=key,
                share_text=str(share_text),
            )
            self._register(task)
            self._start(task)
            return self._snapshot(task)

    def submit_upload(self, filename: str, data: bytes) -> dict[str, Any]:
        extension = safe_upload_extension(filename)
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise VideoImportError("上传内容无效。", status_code=400)
        raw = bytes(data)
        if len(raw) > MAX_VIDEO_BYTES:
            raise VideoImportError("视频不能超过100MB。", status_code=413)
        digest = hashlib.sha256(raw).hexdigest()
        key = f"upload:{extension}:{digest}"
        with self._lock:
            existing = self._existing_for_key(key)
            if existing is not None:
                return self._snapshot(existing)
            self._ensure_slot()
            task = _ImportTask(
                task_id=uuid.uuid4().hex,
                source_kind="upload",
                source_key=key,
                upload_filename=str(filename),
                upload_data=raw,
            )
            self._register(task)
            self._start(task)
            return self._snapshot(task)

    def get(self, task_id: str) -> dict[str, Any]:
        with self._lock:
            task = self._tasks.get(str(task_id))
            if task is None:
                raise VideoImportError("视频任务不存在。", status_code=404)
            return self._snapshot(task)

    def update_draft(self, task_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, Mapping):
            raise VideoImportError("菜谱修改内容无效。", status_code=400)
        with self._lock:
            task = self._tasks.get(str(task_id))
            if task is None:
                raise VideoImportError("视频任务不存在。", status_code=404)
            if task.stage != "review" or task.draft is None:
                raise VideoImportError("当前菜谱还不能修改。", status_code=409)
            incoming = payload.get("draft") if isinstance(payload.get("draft"), Mapping) else payload
            unknown = set(incoming) - _SUPPORTED_DRAFT_FIELDS
            if unknown:
                raise VideoImportError("菜谱修改包含不支持的字段。", status_code=400)
            working = deepcopy(task.draft)
            metadata = working.get("import_metadata") if isinstance(working.get("import_metadata"), Mapping) else {}
            metadata = deepcopy(dict(metadata))
            edits = metadata.setdefault("user_edits", [])
            field_origins = metadata.setdefault("field_origins", {})
            for key, value in incoming.items():
                if key in {"title", "estimated_minutes"}:
                    target_key = "name" if key == "title" else "estimated_time_minutes"
                else:
                    target_key = key
                if target_key in {"name", "estimated_time_minutes", "estimated_minutes", "difficulty"}:
                    if isinstance(value, (dict, list)):
                        raise VideoImportError("菜谱字段格式无效。", status_code=400)
                    new_value = deepcopy(value)
                elif target_key == "servings":
                    if isinstance(value, bool) or not isinstance(value, int) or value <= 0 or value > 6:
                        raise VideoImportError("用餐人数必须是正整数。", status_code=400)
                    new_value = value
                elif target_key in {"ingredients", "steps", "equipment", "notes", "safety_notes"}:
                    if not isinstance(value, list) or len(value) > MAX_DRAFT_LIST_ITEMS:
                        raise VideoImportError("菜谱列表字段格式无效。", status_code=400)
                    new_value = deepcopy(value)
                else:
                    continue
                before = deepcopy(working.get(target_key))
                working[target_key] = new_value
                edit = {"field": target_key, "before": before, "after": deepcopy(new_value), "origin": "user_edit", "evidence": []}
                edits.append(edit)
                field_origins[target_key] = {"origin": "user_edit", "status": "user_edit", "evidence": []}
            # Keep the editable draft shape bounded and discard unknown model
            # fields that could otherwise reach the runtime executor.
            working = _sanitize_recipe_input(working) | {"import_metadata": metadata}
            try:
                reviewed = self._standardize_review_draft(task, working)
            except VideoImportError as exc:
                # Keep a user-editable copy when a field is still incomplete;
                # confirm will report the same safe validation error.  The
                # caller can fill the missing value in a subsequent PATCH.
                task.draft = working
                task.draft_revision += 1
                task.error = str(exc)
            else:
                task.draft = reviewed
                task.draft_revision += 1
                task.error = None
            task.message = "菜谱已更新，请检查后确认。"
            return self._snapshot(task)

    def _complete_review_information(self, task: _ImportTask, draft: Mapping[str, Any]) -> dict[str, Any]:
        missing = _completion_fields_for_draft(draft)
        if not missing:
            return deepcopy(dict(draft))
        metadata = draft.get("import_metadata") if isinstance(draft.get("import_metadata"), Mapping) else {}
        if draft.get("is_tutorial", metadata.get("is_tutorial")) is False:
            return deepcopy(dict(draft))
        dish_count = draft.get("dish_count", metadata.get("dish_count"))
        if isinstance(dish_count, (int, float)) and dish_count > 1:
            return deepcopy(dict(draft))
        started = self.clock()
        self._begin_model_request(task)
        try:
            result = _call_injected_completion_model(
                self.model, _sanitize_recipe_input(draft), missing, _draft_quality_issues(draft),
                title=task.source.title if task.source else "",
            )
        finally:
            self._record_model_request(task, self.model, started)
        working, changed = _merge_completion_draft(draft, _completion_candidate(result, draft), allow_extra_steps="steps" in missing)
        working["import_metadata"] = deepcopy(dict(metadata))
        if len(working.get("steps", [])) != len(draft.get("steps", [])):
            old_steps = draft.get("steps", [])
            new_steps = working.get("steps", [])
            used: set[int] = set()
            remap: dict[int, int] = {}
            for old_index, old_step in enumerate(old_steps):
                for new_index, new_step in enumerate(new_steps):
                    if new_index not in used and old_step.get("instruction") == new_step.get("instruction"):
                        remap[old_index] = new_index
                        used.add(new_index)
                        break
            def remap_path(path: str) -> str:
                return re.sub(r"^steps(?:\[(\d+)\]|\.(\d+))", lambda match: f"steps[{remap.get(int(match[1] or match[2]), int(match[1] or match[2]))}]", path)
            for key in ("field_origins", "field_status"):
                values = working["import_metadata"].get(key)
                if isinstance(values, dict):
                    working["import_metadata"][key] = {remap_path(path): value for path, value in values.items()}
            for key in ("ai_completed_fields", "ai_completions"):
                values = working["import_metadata"].get(key)
                if isinstance(values, list):
                    working["import_metadata"][key] = [remap_path(path) for path in values if isinstance(path, str)]
            for evidence in working["import_metadata"].get("evidence", []):
                if isinstance(evidence, dict) and isinstance(evidence.get("field"), str):
                    evidence["field"] = remap_path(evidence["field"])
        _set_ai_completion_origins(working, changed)
        working["import_metadata"]["completion_metrics"] = {
            "seconds": round(max(0.0, self.clock() - started), 3),
            "usage": _usage_mapping(getattr(self.model, "last_usage", None)),
            "requests": 1,
        }
        if changed:
            working.pop("quality_issues", None)
            working.pop("missing_critical_fields", None)
            working["import_metadata"]["source_quality_issues"] = _draft_quality_issues(draft)
            unresolved_steps = "steps" in missing and len(working.get("steps", [])) <= len(draft.get("steps", [])) and bool(draft.get("steps"))
            if not unresolved_steps:
                working["import_metadata"]["quality_issues"] = []
                working["import_metadata"]["missing_critical_fields"] = []
        try:
            reviewed = self._standardize_review_draft(task, working)
        except VideoImportError:
            reviewed = working
        reviewed["import_metadata"]["completion_metrics"] = working["import_metadata"]["completion_metrics"]
        reviewed["import_metadata"]["completion_needed"] = bool(_completion_fields_for_draft(reviewed))
        reviewed["import_metadata"]["notice"] = "AI 尚未补齐全部信息，可修改草稿或再次补全。" if reviewed["import_metadata"]["completion_needed"] else "缺少的视频信息已由 AI 给出建议。请核对用量、火候和时间后确认保存。"
        return reviewed

    def complete_draft(self, task_id: str) -> dict[str, Any]:
        with self._lock:
            task = self._tasks.get(str(task_id))
            if task is None:
                raise VideoImportError("视频任务不存在。", status_code=404)
            if task.stage != "review" or task.draft is None:
                raise VideoImportError("当前菜谱还不能补全。", status_code=409)
            if task.completion_in_progress:
                raise VideoImportError("AI 正在补全，请稍候。", status_code=409)
            captured = deepcopy(task.draft)
            if not _completion_fields_for_draft(captured):
                task.error = None
                return self._snapshot(task)
            fingerprint = _draft_fingerprint(captured)
            task.completion_in_progress = True
        try:
            completed = self._complete_review_information(task, captured)
            with self._lock:
                if task.stage != "review" or _draft_fingerprint(task.draft) != fingerprint:
                    raise VideoImportError("草稿已修改或任务已结束，已保留当前内容，请再次补全。", status_code=409)
                task.draft = completed
                task.draft_revision += 1
                task.error = None
                task.message = "AI 补全已完成，请核对后确认。"
                return self._snapshot(task)
        finally:
            with self._lock:
                task.completion_in_progress = False

    def confirm(self, task_id: str) -> dict[str, Any]:
        with self._lock:
            task = self._tasks.get(str(task_id))
            if task is None:
                raise VideoImportError("视频任务不存在。", status_code=404)
            if task.stage == "confirmed" and task.draft is not None:
                return {"recipe_id": task.draft.get("recipe_id"), "recipe": deepcopy(task.draft)}
            if task.stage != "review" or task.draft is None or task.source is None or task.media_info is None:
                raise VideoImportError("当前视频还不能确认菜谱。", status_code=409)
            if task.completion_in_progress:
                raise VideoImportError("AI 正在补全，请完成后再确认。", status_code=409)
            draft = deepcopy(task.draft)
            # Metadata quality flags are kept in the transient draft and are
            metadata = draft.get("import_metadata") if isinstance(draft.get("import_metadata"), Mapping) else {}
            # The review snapshot is already the standardized teaching
            # version.  Confirm only validates and persists that exact copy;
            # it must not silently split steps or change timers after the user
            # has inspected it.
            _validate_recipe_quality(draft, metadata)
            self._validate_review_runtime_shape(draft)
            normalized = deepcopy(draft)
            final_metadata = deepcopy(dict(metadata))
            final_metadata["confirmed"] = True
            normalized["import_metadata"] = final_metadata
            normalized["recipe_id"] = f"video_{task.task_id}"
            normalized["source_name"] = "小红书视频" if task.source.platform == "xiaohongshu" else "视频导入"
            normalized["source_url"] = task.source.original_url or task.source.source_url
            try:
                saved = self.store.save(normalized, recipe_id=normalized["recipe_id"])
            except VideoRecipeStoreError as exc:
                raise VideoImportError("菜谱保存失败，请稍后重试。", status_code=500) from exc
            task.draft = saved
            task.stage = "confirmed"
            task.message = "菜谱已确认，可以开始逐步教学。"
            task.error = None
            if self._active_task_id == task.task_id:
                self._active_task_id = None
            return {"recipe_id": saved["recipe_id"], "recipe": deepcopy(saved)}

    def cancel(self, task_id: str) -> dict[str, Any]:
        with self._lock:
            task = self._tasks.get(str(task_id))
            if task is None:
                raise VideoImportError("视频任务不存在。", status_code=404)
            if task.stage in {"confirmed", "cancelled", "failed"}:
                return self._snapshot(task)
            task.cancel_event.set()
            self._finish_processing(task)
            task.stage = "cancelled"
            task.message = "视频导入已取消。"
            task.error = None
            task.draft = None
            if self._active_task_id == task.task_id:
                self._active_task_id = None
            return self._snapshot(task)

    def list_recipes(self) -> list[dict[str, Any]]:
        try:
            return self.store.list_recipes()
        except Exception as exc:
            raise VideoImportError("已导入菜谱暂时无法读取。", status_code=500) from exc

    def recipe_detail(self, recipe_id: str, servings: int = 1) -> dict[str, Any]:
        try:
            return self.store.detail(recipe_id, servings=servings)
        except KeyError as exc:
            raise VideoImportError("菜谱不存在。", status_code=404) from exc
        except VideoRecipeStoreError as exc:
            raise VideoImportError(str(exc), status_code=400) from exc

    def wait(self, task_id: str, timeout: float | None = None) -> dict[str, Any]:
        """Test/integration convenience: wait for the worker, then snapshot."""

        with self._lock:
            task = self._tasks.get(str(task_id))
            if task is None:
                raise VideoImportError("视频任务不存在。", status_code=404)
            future = task.future
        if future is not None:
            try:
                future.join(timeout) if isinstance(future, threading.Thread) else future.result(timeout=timeout)
            except (TimeoutError, Exception) as exc:
                if isinstance(exc, TimeoutError):
                    return self.get(task_id)
        return self.get(task_id)

    def shutdown(self, wait: bool = False) -> None:
        with self._lock:
            threads = list(self._threads)
        if wait:
            for thread in threads:
                thread.join(timeout=5)

    def _register(self, task: _ImportTask) -> None:
        task.submitted_at = self.clock()
        self._tasks[task.task_id] = task
        self._dedupe[task.source_key] = task.task_id
        self._active_task_id = task.task_id

    def _existing_for_key(self, key: str) -> _ImportTask | None:
        task_id = self._dedupe.get(key)
        task = self._tasks.get(task_id) if task_id else None
        # A terminal failure or cancellation must not poison retries for the
        # same source.  Keep confirmed tasks idempotent, while allowing a
        # fresh task id after the caller fixes the source or configuration.
        if task is not None and task.stage in {"failed", "cancelled"}:
            return None
        return task

    def _ensure_slot(self) -> None:
        if self._active_task_id is None:
            return
        active = self._tasks.get(self._active_task_id)
        if active is None or active.stage in {"confirmed", "failed", "cancelled"}:
            self._active_task_id = None
            return
        raise VideoImportError("已有视频导入任务正在处理，请稍候。", status_code=429)

    def _start(self, task: _ImportTask) -> None:
        thread = threading.Thread(target=self._run_task, args=(task,), name=f"video-import-{task.task_id}", daemon=True)
        task.future = thread
        self._threads.add(thread)
        thread.start()

    def _check_cancel_or_timeout(self, task: _ImportTask) -> None:
        if task.cancel_event.is_set():
            raise VideoImportCancelled()
        if task.timed_out or task.deadline is not None and self.clock() > task.deadline:
            raise VideoImportError("视频处理超时，请上传较短视频或稍后重试。", status_code=504)

    @staticmethod
    def _metric_stage(stage: str) -> str | None:
        return {"acquiring": "fetching", "analyzing": "analyzing", "structuring": "structuring"}.get(stage)

    def _advance_metric_phase(self, task: _ImportTask, phase: str | None) -> None:
        """Close the previous processing phase and start ``phase``."""

        now = self.clock()
        with task.lock:
            current = task.metric_phase
            if current is not None and task.metric_phase_started_at is not None:
                task.stage_seconds[current] = task.stage_seconds.get(current, 0.0) + max(
                    0.0, now - task.metric_phase_started_at
                )
            task.metric_phase = phase
            task.metric_phase_started_at = now if phase is not None else None

    def _finish_processing(self, task: _ImportTask) -> None:
        self._advance_metric_phase(task, None)
        with task.lock:
            if task.finished_at is None:
                task.finished_at = self.clock()

    def _record_model_request(self, task: _ImportTask, model: Any, started_at: float) -> None:
        elapsed = max(0.0, self.clock() - started_at)
        usage = _usage_mapping(getattr(model, "last_usage", None))
        with task.lock:
            # A watchdog/cancel may have frozen the terminal snapshot while a
            # provider call is still unwinding.  Do not mutate terminal
            # metrics from that late result.
            if task.finished_at is not None:
                return
            task.model_seconds += elapsed
            for target_key, source_keys in (
                ("input_tokens", ("prompt_tokens", "input_tokens")),
                ("output_tokens", ("completion_tokens", "output_tokens")),
            ):
                for source_key in source_keys:
                    value = usage.get(source_key)
                    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(float(value)) or value < 0:
                        continue
                    task_value = int(value)
                    previous = getattr(task, target_key)
                    setattr(task, target_key, task_value if previous is None else previous + task_value)
                    break

    @staticmethod
    def _begin_model_request(task: _ImportTask) -> None:
        with task.lock:
            if task.finished_at is None:
                task.model_requests += 1

    def _publish_partial(self, task: _ImportTask, preview: Mapping[str, Any]) -> None:
        """Store a small, throttled, non-final preview for polling clients."""

        compact = _compact_partial_preview(preview)
        if compact is None:
            return
        now = self.clock()
        with task.lock:
            if task.cancel_event.is_set() or task.stage in {"cancelled", "failed", "confirmed"}:
                return
            if (
                task.partial_preview_updated_at is not None
                and now - task.partial_preview_updated_at < PARTIAL_PREVIEW_INTERVAL_SECONDS
            ):
                return
            if compact == task.partial_preview:
                return
            task.partial_preview = compact
            task.partial_preview_updated_at = now
            if task.first_preview_at is None:
                task.first_preview_at = now
            if task.first_step_at is None and compact.get("steps"):
                task.first_step_at = now

    def _set_stage(self, task: _ImportTask, stage: str, message: str) -> None:
        with self._lock:
            if task.cancel_event.is_set() or task.stage == "cancelled":
                raise VideoImportCancelled()
            if task.timed_out or task.stage == "failed":
                raise VideoImportError("视频处理超时，请上传较短视频或稍后重试。", status_code=504)
            task.stage = stage
            task.message = message
            task.error = None
            self._advance_metric_phase(task, self._metric_stage(stage))

    def _run_task(self, task: _ImportTask) -> None:
        watchdog: threading.Timer | None = None
        try:
            task.started_at = self.clock()
            task.deadline = task.started_at + self.task_timeout_seconds
            # A blocked network/model call cannot be interrupted safely from a
            # worker thread.  The watchdog marks the task expired so any late
            # result is discarded at the next checkpoint and never reaches a
            # review draft or store.
            watchdog = threading.Timer(self.task_timeout_seconds, self._mark_timed_out, args=(task,))
            watchdog.daemon = True
            watchdog.start()
            self._set_stage(task, "acquiring", "正在读取视频来源。")
            source = self._acquire_source(task)
            task.source = source
            self._check_cancel_or_timeout(task)
            self._set_stage(task, "analyzing", "正在理解视频画面和声音。")
            evidence, raw_recipe = self._analyze_source(task, source)
            self._check_cancel_or_timeout(task)
            self._advance_metric_phase(task, None)
            self._advance_metric_phase(task, "structuring")
            try:
                normalized_draft = self._prepare_review_draft(task, source, evidence, raw_recipe)
                if _completion_fields_for_draft(normalized_draft):
                    self._set_stage(task, "structuring", "正在用 AI 补全缺失用量和做法。")
                    try:
                        normalized_draft = self._complete_review_information(task, normalized_draft)
                    except VideoImportError as exc:
                        normalized_draft["import_metadata"]["completion_needed"] = True
                        normalized_draft["import_metadata"]["notice"] = f"AI 补全暂未完成：{exc} 可修改草稿或使用 AI 补全。"
            finally:
                self._advance_metric_phase(task, None)
            with self._lock:
                if task.cancel_event.is_set() or task.stage == "cancelled":
                    raise VideoImportCancelled()
                if task.timed_out or task.stage == "failed":
                    raise VideoImportError("视频处理超时，请上传较短视频或稍后重试。", status_code=504)
                task.draft = normalized_draft
                task.draft_revision += 1
                task.stage = "review"
                task.message = "菜谱已整理，请检查 AI 补全内容后确认。"
                task.error = None
                if task.finished_at is None:
                    task.finished_at = self.clock()
        except VideoImportCancelled:
            with self._lock:
                self._finish_processing(task)
                if task.stage != "failed":
                    task.stage = "cancelled"
                    task.message = "视频导入已取消。"
                    task.error = None
                    task.draft = None
                    if self._active_task_id == task.task_id:
                        self._active_task_id = None
        except VideoImportError as exc:
            with self._lock:
                self._finish_processing(task)
                if task.stage not in {"cancelled", "failed"}:
                    task.failed_stage = task.stage
                    task.stage = "failed"
                    task.message = "视频导入未完成。"
                    task.error = str(exc)
                    task.draft = None
                    if self._active_task_id == task.task_id:
                        self._active_task_id = None
        except Exception as exc:
            with self._lock:
                self._finish_processing(task)
                if task.stage not in {"cancelled", "failed"}:
                    task.failed_stage = task.stage
                    task.stage = "failed"
                    task.message = "视频导入未完成。"
                    task.error = self._safe_worker_error(task)
                    task.draft = None
                    if self._active_task_id == task.task_id:
                        self._active_task_id = None
        finally:
            if watchdog is not None:
                watchdog.cancel()
            self._cleanup_task_files(task)
            with self._lock:
                current = threading.current_thread()
                self._threads.discard(current)

    def _mark_timed_out(self, task: _ImportTask) -> None:
        """Fail and release a task even when its current call is blocked."""

        with self._lock:
            if task.stage in {"confirmed", "cancelled", "failed"}:
                return
            self._finish_processing(task)
            task.timed_out = True
            task.failed_stage = task.stage
            task.stage = "failed"
            task.message = "视频处理超时。"
            task.error = "视频处理超时，请上传较短视频或稍后重试。"
            task.draft = None
            if self._active_task_id == task.task_id:
                self._active_task_id = None

    def _acquire_source(self, task: _ImportTask) -> VideoSource:
        if task.source_kind == "upload":
            if task.upload_data is None or task.upload_filename is None:
                raise VideoImportError("上传内容无效。", status_code=400)
            task.temp_dir = create_task_temp_dir(self.temp_root, task.task_id)
            path = write_upload(task.upload_data, task.temp_dir / "input", filename=task.upload_filename)
            source = VideoSource(
                title=Path(task.upload_filename).stem,
                source_url=None,
                platform="upload",
            )
        else:
            adapter = self.adapter
            resolve = next((getattr(adapter, name, None) for name in ("resolve", "fetch", "acquire") if callable(getattr(adapter, name, None))), None)
            if resolve is None:
                raise VideoImportError("视频链接通道不可用，请上传视频文件。", status_code=503)
            source_result = resolve(task.share_text or "")
            if isinstance(source_result, VideoSource):
                source = source_result
            elif isinstance(source_result, Mapping):
                source = _source_from_mapping(source_result, fallback_url=extract_share_url(task.share_text or ""), platform="xiaohongshu")
            elif isinstance(source_result, (tuple, list)) and source_result:
                first = source_result[0]
                details = source_result[1] if len(source_result) > 1 and isinstance(source_result[1], Mapping) else {}
                source = _source_from_mapping({**details, "video_path": first}, fallback_url=extract_share_url(task.share_text or ""), platform="xiaohongshu")
            elif isinstance(source_result, (str, Path)):
                source = _source_from_mapping({"video_path": source_result}, fallback_url=extract_share_url(task.share_text or ""), platform="xiaohongshu")
            else:
                raise VideoImportError("视频链接通道返回内容无效，请上传视频文件。", status_code=502)
            task.temp_dir = create_task_temp_dir(self.temp_root, task.task_id)
            if source.video_bytes is not None:
                if len(source.video_bytes) > MAX_VIDEO_BYTES:
                    raise VideoImportError("视频不能超过100MB。", status_code=413)
                extension = ".mov" if str(source.content_type or "").lower().endswith("quicktime") else ".mp4"
                path = write_upload(source.video_bytes, task.temp_dir / f"input{extension}")
            elif source.video_path is not None:
                try:
                    local_bytes = source.video_path.read_bytes()
                except (OSError, ValueError) as exc:
                    raise VideoImportError("视频文件无法读取，请上传视频文件。", status_code=400) from exc
                if len(local_bytes) > MAX_VIDEO_BYTES:
                    raise VideoImportError("视频不能超过100MB。", status_code=413)
                extension = source.video_path.suffix.lower() if source.video_path.suffix.lower() in {".mp4", ".mov"} else ".mp4"
                path = write_upload(local_bytes, task.temp_dir / f"input{extension}")
            elif source.video_url:
                try:
                    if self.downloader is not None:
                        downloaded = self.downloader(source.video_url, task.temp_dir / "input.mp4")
                        path = Path(downloaded or task.temp_dir / "input.mp4")
                    else:
                        path = download_public_video(source.video_url, task.temp_dir / "input.mp4")
                except VideoMediaError as exc:
                    raise VideoImportError(str(exc), status_code=getattr(exc, "status_code", 502)) from exc
            else:
                raise VideoImportError("来源没有可下载的视频，请上传视频文件。", status_code=422)
        self._check_cancel_or_timeout(task)
        try:
            if self.media_validator is not None:
                result = self.media_validator(path)
                if isinstance(result, MediaInfo):
                    info = result
                elif isinstance(result, Mapping):
                    duration_value = result.get("duration_seconds") or result.get("duration")
                    info = MediaInfo(
                        path=path,
                        extension=str(result.get("extension") or path.suffix.lower()),
                        format_name=str(result.get("format") or "mp4"),
                        duration_seconds=float(duration_value or 0),
                        size_bytes=path.stat().st_size,
                        mime_type=str(result.get("mime_type") or "video/mp4"),
                    )
                else:
                    duration_value = getattr(result, "duration_seconds", None) or getattr(result, "duration", None)
                    info = MediaInfo(
                        path=path,
                        extension=path.suffix.lower(),
                        format_name="mp4",
                        duration_seconds=float(duration_value or 0),
                        size_bytes=path.stat().st_size,
                        mime_type="video/mp4",
                    )
            else:
                info = probe_video_file(path)
        except VideoMediaError as exc:
            raise VideoImportError(str(exc), status_code=getattr(exc, "status_code", 415)) from exc
        except (OSError, ValueError, TypeError) as exc:
            raise VideoImportError("视频格式校验失败，请上传 MP4 或 MOV 文件。", status_code=415) from exc
        task.media_info = info
        if info.duration_seconds > MAX_VIDEO_SECONDS + 0.001:
            raise VideoImportError("视频时长不能超过3分钟。", status_code=413)
        source.duration_seconds = info.duration_seconds
        return source

    def _analyze_source(self, task: _ImportTask, source: VideoSource) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        if task.temp_dir is None or task.media_info is None:
            raise VideoImportError("视频文件不可用，请重试。", status_code=500)
        model = self.model
        if model is None or (hasattr(model, "is_available") and not model.is_available()):
            raise VideoImportError("未配置视频分析服务，请直接上传视频或配置千问 Key。", status_code=503)
        compression_started = self.clock()
        segments = self._make_segments(task)
        with task.lock:
            task.compression_seconds += max(0.0, self.clock() - compression_started)
        evidence: list[dict[str, Any]] = []
        segment_outputs: list[dict[str, Any]] = []
        partial_callback = lambda preview: self._publish_partial(task, preview)
        for index, (path, start, end) in enumerate(segments):
            self._check_cancel_or_timeout(task)
            data_url = self._video_data_url(path)
            prompt = self._evidence_prompt(
                source,
                start=start,
                end=end,
                index=index,
                total=len(segments),
                complete_recipe=len(segments) == 1,
            )
            model_started = self.clock()
            self._begin_model_request(task)
            try:
                result = _call_injected_model(model, data_url, prompt, partial_callback=partial_callback)
            finally:
                self._record_model_request(task, model, model_started)
            segment_outputs.append(result)
            evidence.extend(_evidence_items(result, segment_start=start, segment_end=end))
        # A single short video can produce the full recipe in one response;
        # this avoids uploading the same visual/audio payload a second time.
        # Multi-segment evidence must always be merged so later segments cannot
        # be silently discarded just because one segment returned a recipe.
        if len(segments) == 1:
            direct = _recipe_from_model(segment_outputs[0])
            if direct is not None:
                return evidence, direct
        self._check_cancel_or_timeout(task)
        # The aggregate evidence already contains the timestamped facts.  Do
        # not send each segment's nested evidence array a second time in the
        # observations payload; that duplication adds input tokens without
        # adding signal for the text-only organizer.
        compact_observations: list[dict[str, Any]] = []
        for result in segment_outputs:
            compact = dict(result)
            for key in ("evidence", "observations", "field_evidence"):
                compact.pop(key, None)
            compact_observations.append(compact)
        evidence_text = json.dumps(
            {"evidence": evidence, "observations": compact_observations},
            ensure_ascii=False,
            separators=(",", ":"),
        )[:PARTIAL_PREVIEW_MAX_TEXT]
        organizer_prompt = self._organizer_prompt(source)
        model_started = self.clock()
        self._begin_model_request(task)
        try:
            organized = _call_injected_text_model(
                model,
                evidence_text,
                organizer_prompt,
                partial_callback=partial_callback,
            )
        finally:
            self._record_model_request(task, model, model_started)
        recipe = _recipe_from_model(organized) or _evidence_to_recipe(organized, source)
        if not isinstance(recipe, dict):
            recipe = _evidence_to_recipe(segment_outputs[0], source)
        # Carry model quality flags into the gate and preserve all raw evidence
        # in import metadata, while never forwarding arbitrary model fields.
        for key in ("is_tutorial", "dish_count", "quality_issues", "missing_critical_fields", "confidence"):
            if key in organized and key not in recipe:
                recipe[key] = deepcopy(organized[key])
        return evidence, recipe

    def _make_segments(self, task: _ImportTask) -> list[tuple[Path, float, float]]:
        assert task.temp_dir is not None and task.media_info is not None
        path = task.media_info.path
        duration = task.media_info.duration_seconds
        model_raw_limit = model_payload_size(0)
        if duration <= 120 and path.stat().st_size <= model_raw_limit:
            return [(path, 0.0, duration)]
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            try:
                import imageio_ffmpeg  # type: ignore

                ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
            except Exception:
                ffmpeg = None
        if not ffmpeg:
            raise VideoImportError("视频过大或需要分段，但当前环境缺少 FFmpeg。", status_code=503)
        starts: list[float] = []
        start = 0.0
        while start < duration:
            starts.append(start)
            if start + 120 >= duration:
                break
            start += 115  # 120-second windows with a 5-second overlap.
        segments: list[tuple[Path, float, float]] = []
        for index, start in enumerate(starts):
            end = min(duration, start + 120)
            target = task.temp_dir / f"segment-{index}.mp4"
            def transcode_command(output: Path, long_edge: int) -> list[str]:
                # Keep both dimensions bounded.  The former min(iw, 720)
                # expression left a portrait video at its original height,
                # making the bundled FFmpeg spend a minute encoding frames we
                # never send to the model.  ``-2`` preserves aspect ratio and
                # rounds the inferred side to an even value for H.264.
                scale = (
                    f"w='if(gt(iw,ih),{long_edge},if(lt(iw,ih),-2,{long_edge}))'"
                    f":h='if(gt(ih,iw),{long_edge},if(lt(ih,iw),-2,{long_edge}))'"
                )
                return [
                    str(ffmpeg), "-y", "-hide_banner", "-loglevel", "error",
                    "-ss", f"{start:.3f}", "-i", str(path), "-t", f"{end - start:.3f}",
                    "-map", "0:v:0", "-map", "0:a:0?",
                    "-vf", f"fps=5,scale={scale}", "-r", "5",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "32", "-threads", "2",
                    "-c:a", "aac", "-b:a", "64k", "-shortest", "-movflags", "+faststart", str(output),
                ]

            command = transcode_command(target, 720)
            try:
                subprocess.run(command, capture_output=True, timeout=60, check=True)
            except (OSError, subprocess.SubprocessError) as exc:
                raise VideoImportError("视频分段失败，请上传较短视频。", status_code=422) from exc
            if not target.exists() or target.stat().st_size <= 0 or target.stat().st_size > model_raw_limit:
                # A failed size check is retried once at a smaller scale.
                smaller = task.temp_dir / f"segment-{index}-small.mp4"
                smaller_command = transcode_command(smaller, 480)
                try:
                    subprocess.run(smaller_command, capture_output=True, timeout=60, check=True)
                except (OSError, subprocess.SubprocessError) as exc:
                    raise VideoImportError("视频无法压缩到模型输入限制，请上传较短视频。", status_code=422) from exc
                target = smaller
            if target.exists() and target.stat().st_size > 0 and target.stat().st_size <= model_raw_limit:
                segments.append((target, start, end))
            else:
                raise VideoImportError("视频无法压缩到模型输入限制，请上传较短视频。", status_code=413)
        return segments

    @staticmethod
    def _video_data_url(path: Path) -> str:
        raw = path.read_bytes()
        encoded = base64.b64encode(raw).decode("ascii")
        if len(encoded) >= MAX_MODEL_BASE64_BYTES:
            raise VideoImportError("视频片段超过模型输入限制。", status_code=413)
        extension = path.suffix.lower()
        mime = "video/quicktime" if extension == ".mov" else "video/mp4"
        return f"data:{mime};base64,{encoded}"

    @staticmethod
    def _evidence_prompt(
        source: VideoSource,
        *,
        start: float,
        end: float,
        index: int,
        total: int,
        complete_recipe: bool = False,
    ) -> str:
        output_name = "name" if complete_recipe else "title"
        completion_note = (
            "这是短视频单段任务，请同时输出完整可执行菜谱；不要等待第二轮整理。"
            if complete_recipe
            else "这是分段事实提取，只记录该段观察到的内容。"
        )
        return (
            "你是菜谱事实提取器。先根据视频画面和声音提取事实；视频明确的量、火力、时长放入证据。"
            "视频未说明人数时 servings 默认为 1，推测用量也按一人份计算；已明确人数和用量保留原值。"
            + (
                "这是单段完整菜谱任务：视频未明确的用量、火力、时长或关键步骤，按常见家常做法给出安全可执行的 AI 建议，"
                "并把每个建议字段路径列入 ai_completion_fields；不要声称 AI 建议来自视频。"
                "视频已有适量、少量或一圈等用量表述可以直接保留；不要为了数字化而伪造明确量。"
                "主要肉类（鸡腿肉、牛肉、鱼虾等）优先给具体克数或个数；视频未明确时按 servings 人数估算，并标为 AI 建议。调料可保留适量。"
                "AI 字段写完整索引路径，如 ingredients[8].amount、steps[1].duration_seconds，不要写笼统路径。"
                if complete_recipe
                else "这是分段事实提取任务：视频未明确的字段留空，暂不猜测，交给后续整理。"
            )
            + "不要把 AI 建议伪装成视频事实。"
            "证据只保留关键用量、计时、火力的短值和原视频秒数；只输出 JSON："
            f'{{"{output_name}":string,"servings":integer,"estimated_time_minutes":integer|null,"difficulty":"简单"|"中等",'
            '"ingredients":[{"name":string,"amount":number|string,"unit":string,"optional":boolean}],'
            '"steps":[{"instruction":string,"duration_seconds":number|null,"heat_level":string|null,"safety_note":string|null}],'
            '"evidence":[{"field":string,"value":string|number,"start_seconds":number,"end_seconds":number,"confidence":string}],'
            '"quality_issues":[string],"ai_completion_fields":[string],"is_tutorial":boolean,"dish_count":integer}。'
            f"这是第{index + 1}/{total}段，原视频时间范围 {start:.1f}-{end:.1f} 秒。"
            f"{completion_note}视频标题：{source.title[:160]}；描述仅作辅助：{source.description[:400]}。"
        )

    @staticmethod
    def _organizer_prompt(source: VideoSource) -> str:
        return (
            "你是新手厨房教学菜谱整理器。把全部分段事实合并成一道菜的可执行菜谱。"
            "未说明人数时 servings 默认为 1，推测用量按一人份计算。"
            "只输出 JSON；视频明确内容标为视频事实并保留证据。无法从事实确定的量、火力、时长或关键步骤，"
            "按常见家常做法给出安全可执行的 AI 建议，并把每个建议字段路径写入 ai_completion_fields，"
            "适量、少量或一圈等视频原有用量可以保留。AI 字段使用完整索引路径。"
            "主要肉类尽量给具体克数或个数；没有视频明确量时按人数估算，标为 AI 建议。调料可保留适量。"
            "不要声称 AI 建议来自视频。"
            "步骤保留切配、调味、加热、等待、装盘顺序，保留视频明确的计时。"
            '{"name":string,"servings":integer,"estimated_time_minutes":integer|null,"difficulty":"简单"|"中等",'
            '"ingredients":[{"name":string,"amount":number|string,"unit":string,"optional":boolean}],'
            '"equipment":[string],"notes":[string],"steps":[{"instruction":string,"duration_seconds":number|null,"heat_level":string|null,"safety_note":string|null}],'
            '"is_tutorial":boolean,"dish_count":integer,"quality_issues":[string],"ai_completion_fields":[string]}。'
            f"原视频标题：{source.title[:160]}。描述仅作辅助：{source.description[:400]}。"
        )

    def _prepare_review_draft(
        self,
        task: _ImportTask,
        source: VideoSource,
        evidence: list[dict[str, Any]],
        raw_recipe: Mapping[str, Any],
    ) -> dict[str, Any]:
        draft = _sanitize_recipe_input(raw_recipe)
        # The title/description comes from the source only as context; the
        # model's chosen recipe name remains editable and is what gets taught.
        draft.setdefault("servings", 1)
        if not isinstance(draft.get("servings"), int) or isinstance(draft.get("servings"), bool) or draft["servings"] <= 0:
            draft["servings"] = 1
        field_origins: dict[str, Any] = {}
        for item in evidence:
            field = str(item.get("field") or "")
            if field:
                field_origins[field] = {
                    "origin": "video_fact",
                    "status": "fact",
                    "evidence": [deepcopy(item)],
                }
        ai_fields = raw_recipe.get("ai_completion_fields") if isinstance(raw_recipe.get("ai_completion_fields"), list) else []
        expanded_fields: list[str] = []
        for field in ai_fields:
            group = re.fullmatch(r"(ingredients|steps)\.([a-z_]+)", str(field))
            if group:
                expanded_fields.extend(f"{group[1]}[{index}].{group[2]}" for index, _ in enumerate(draft.get(group[1], [])))
            else:
                expanded_fields.append(str(field))
        ai_fields = [field for field in expanded_fields if field not in field_origins]
        for field in ai_fields:
            field_text = _safe_text(field, limit=160)
            if field_text:
                field_origins[field_text] = {"origin": "ai_completion", "status": "ai_completion", "evidence": []}
        draft["import_metadata"] = {
            "confirmed": False,
            "source": {
                **source.safe_metadata(),
                "duration_seconds": task.media_info.duration_seconds if task.media_info else source.duration_seconds,
            },
            "evidence": deepcopy(evidence),
            "field_origins": field_origins,
            "field_status": deepcopy(field_origins),
            "ai_completions": [_safe_text(item, limit=160) for item in ai_fields if _safe_text(item, limit=160)],
            "user_edits": [],
            "rule_adjustments": [],
            "standardization_diff": [],
            "servings_origin": "ai_completion" if not raw_recipe.get("servings") else "video_fact",
            "is_tutorial": raw_recipe.get("is_tutorial", True),
            "dish_count": raw_recipe.get("dish_count", 1),
            "quality_issues": deepcopy(raw_recipe.get("quality_issues") if isinstance(raw_recipe.get("quality_issues"), list) else []),
            "missing_critical_fields": deepcopy(raw_recipe.get("missing_critical_fields") if isinstance(raw_recipe.get("missing_critical_fields"), list) else []),
            "ai_completed": bool(ai_fields),
            "ai_completed_fields": [
                _safe_text(item, limit=160)
                for item in ai_fields
                if _safe_text(item, limit=160)
            ],
        }
        draft["import_metadata"]["completion_needed"] = bool(_missing_completion_fields(draft))
        # Imported model output can contain guessed timers and sparse
        # provenance.  Apply the evidence policy before attempting runtime
        # normalization so the review fallback is just as honest as the
        # successful normalized path.
        _apply_import_timing_policy(draft, draft, draft["import_metadata"])
        _ensure_import_field_origins(draft)
        try:
            return self._standardize_review_draft(task, draft)
        except VideoImportError:
            # Keep the unstandardized draft visible so the user can supply a
            # missing amount or key step; confirm remains blocked until the
            # complete draft can be normalized.
            return draft

    def _standardize_review_draft(self, task: _ImportTask, draft: Mapping[str, Any]) -> dict[str, Any]:
        if task.source is None or task.media_info is None:
            raise VideoImportError("视频来源尚未准备好，请稍后重试。", status_code=409)
        raw = _sanitize_recipe_input(draft)
        # Carry transient quality flags through standardization; they are used
        # by confirmation but never sent to the runtime executor.
        source_metadata = draft.get("import_metadata") if isinstance(draft.get("import_metadata"), Mapping) else {}
        _apply_import_timing_policy(raw, raw, source_metadata)
        try:
            normalized = self._normalize_for_runtime(raw)
        except VideoImportError:
            raise
        _apply_import_timing_policy(raw, normalized, source_metadata)
        _strip_runtime_steps(normalized)
        evidence = source_metadata.get("evidence") if isinstance(source_metadata.get("evidence"), list) else []
        user_edits = source_metadata.get("user_edits") if isinstance(source_metadata.get("user_edits"), list) else []
        diff = _diff_values(raw, normalized)
        final_metadata = _build_import_metadata(
            task.source,
            media_info=task.media_info,
            evidence=evidence,
            draft=raw,
            normalized=normalized,
            user_edits=user_edits,
            diff=diff,
        )
        for key in ("is_tutorial", "dish_count", "quality_issues", "missing_critical_fields"):
            if key in source_metadata:
                final_metadata[key] = deepcopy(source_metadata[key])
        # Preserve explicit model/user completion provenance across the fresh
        # metadata build. Fields without evidence are already AI-labelled by
        # _build_import_metadata; only the explicit completion list may
        # override a matching evidence path.
        final_metadata["ai_completed_fields"] = deepcopy(
            source_metadata.get("ai_completed_fields")
            if isinstance(source_metadata.get("ai_completed_fields"), list)
            else []
        )
        final_metadata["ai_completed"] = bool(
            source_metadata.get("ai_completed")
            or final_metadata["ai_completed_fields"]
        )
        final_metadata["completion_needed"] = bool(_missing_completion_fields(normalized))
        normalized["import_metadata"] = final_metadata
        _set_ai_completion_origins(normalized, final_metadata["ai_completed_fields"])
        _ensure_import_field_origins(normalized)
        return normalized

    @staticmethod
    def _validate_review_runtime_shape(recipe: Mapping[str, Any]) -> None:
        """Validate the already displayed runtime recipe without normalizing."""

        if not isinstance(recipe.get("name"), str) or not recipe.get("name", "").strip():
            raise VideoImportError("菜名不能为空，请修改后再确认。", status_code=422)
        servings = recipe.get("servings")
        if isinstance(servings, bool) or not isinstance(servings, int) or servings < 1 or servings > 6:
            raise VideoImportError("用餐人数必须在1到6人之间。", status_code=422)
        ingredients = recipe.get("ingredients")
        if not isinstance(ingredients, list) or not ingredients:
            raise VideoImportError("请补全食材后再确认。", status_code=422)
        for item in ingredients:
            if not isinstance(item, Mapping) or not str(item.get("name") or "").strip() or _is_missing_recipe_value(item.get("amount"), text=True):
                raise VideoImportError("请补全食材用量后再确认。", status_code=422)
            if _is_missing_recipe_unit(item.get("unit")) and not _contains_vague_amount(item.get("amount")):
                raise VideoImportError("请补全食材单位后再确认。", status_code=422)
        steps = recipe.get("steps")
        if not isinstance(steps, list) or not steps or any(not isinstance(step, Mapping) or not str(step.get("instruction") or "").strip() for step in steps):
            raise VideoImportError("请补全步骤后再确认。", status_code=422)
        for step in steps:
            duration = step.get("duration_seconds")
            if duration is not None and (isinstance(duration, bool) or not isinstance(duration, (int, float)) or not math.isfinite(float(duration)) or duration <= 0):
                raise VideoImportError("步骤计时字段无效，请修改后再确认。", status_code=422)

    def _normalize_for_runtime(self, draft: Mapping[str, Any]) -> dict[str, Any]:
        raw = _sanitize_recipe_input(draft)
        if any(_is_missing_recipe_value(item.get("amount"), text=True) for item in raw.get("ingredients", [])):
            raise VideoImportError("部分食材用量缺失，请使用 AI 补全后再确认。", status_code=422)
        # A missing serving count is a stated AI/default completion, but the
        # review draft always exposes the assumption as 1 person.
        if not raw.get("servings"):
            raw["servings"] = 1
        normalizer = self.normalizer or _normalizer()
        try:
            normalized = normalizer.normalize(raw, servings=int(raw["servings"]))
        except Exception as exc:
            raise VideoImportError("菜谱用量或步骤仍不完整，请修改后再确认。", status_code=422) from exc
        if not isinstance(normalized, dict):
            raise VideoImportError("菜谱标准化结果无效，请修改后再确认。", status_code=422)
        # Qualitative source amounts are valid. Truly blank source amounts
        # were checked above so normalization cannot silently invent “适量”.
        for item in normalized.get("ingredients") if isinstance(normalized.get("ingredients"), list) else []:
            if not isinstance(item, Mapping) or _is_missing_recipe_value(item.get("amount"), text=True):
                raise VideoImportError("部分食材用量无法确认，请补全后再确认。", status_code=422)
        return normalized

    @staticmethod
    def _cleanup_task_files(task: _ImportTask) -> None:
        try:
            directory = task.temp_dir
            if directory is not None and directory.name.startswith("video-import-") and directory.is_dir():
                shutil.rmtree(directory)
        except OSError:
            pass
        finally:
            # The review draft retains only safe source metadata.  Release
            # upload and signed media payloads as soon as the worker exits so
            # a sequence of 100MB imports cannot accumulate in _tasks.
            task.temp_dir = None
            task.upload_data = None
            if task.source is not None:
                task.source.video_bytes = None
                task.source.video_url = None
                task.source.video_path = None

    @staticmethod
    def _safe_worker_error(task: _ImportTask) -> str:
        return "视频导入失败，请检查视频格式后重试或直接上传 MP4/MOV。"

    def _snapshot(self, task: _ImportTask) -> dict[str, Any]:
        now = self.clock()
        with task.lock:
            stage_seconds = dict(task.stage_seconds)
            if task.metric_phase is not None and task.metric_phase_started_at is not None and task.finished_at is None:
                stage_seconds[task.metric_phase] = stage_seconds.get(task.metric_phase, 0.0) + max(
                    0.0, now - task.metric_phase_started_at
                )
            finished_at = task.finished_at
            elapsed_end = finished_at if finished_at is not None else now
            submitted_at = task.submitted_at if task.submitted_at is not None else elapsed_end
            elapsed = max(0.0, elapsed_end - submitted_at)
            first_preview = (
                max(0.0, task.first_preview_at - submitted_at)
                if task.first_preview_at is not None
                else None
            )
            first_step = (
                max(0.0, task.first_step_at - submitted_at)
                if task.first_step_at is not None
                else None
            )
            metrics: dict[str, Any] = {
                "elapsed_seconds": round(elapsed, 3),
                "stage_seconds": {key: round(max(0.0, value), 3) for key, value in stage_seconds.items()},
                "first_preview_seconds": round(first_preview, 3) if first_preview is not None else None,
                "first_step_seconds": round(first_step, 3) if first_step is not None else None,
                "model_requests": int(task.model_requests),
                "compression_seconds": round(max(0.0, task.compression_seconds), 3),
                "model_seconds": round(max(0.0, task.model_seconds), 3),
                "input_tokens": task.input_tokens,
                "output_tokens": task.output_tokens,
            }
            return {
                "id": task.task_id,
                "stage": task.stage,
                "message": task.message,
                "draft": deepcopy(task.draft),
                "error": task.error,
                "failed_stage": task.failed_stage,
                "partial_preview": deepcopy(task.partial_preview),
                "metrics": metrics,
            }


__all__ = [
    "CompositeXiaohongshuAdapter",
    "PublicXiaohongshuAdapter",
    "QwenVideoModel",
    "VideoImportError",
    "VideoImportService",
    "VideoSource",
    "WellbyteXiaohongshuAdapter",
    "canonical_note_url",
    "extract_share_url",
    "parse_xiaohongshu_html",
]
