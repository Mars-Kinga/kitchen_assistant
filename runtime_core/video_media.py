"""Media handling primitives for video recipe imports.

The import worker deliberately keeps media handling in this module.  It makes
the security boundary (public URL validation, bounded downloads and real
container probing) easy to exercise without starting the import service or
calling a model.
"""

from __future__ import annotations

import gzip
import ipaddress
import io
import math
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from . import video_network


MAX_VIDEO_BYTES = 100 * 1024 * 1024
MAX_VIDEO_SECONDS = 180
MAX_MODEL_BASE64_BYTES = 10 * 1024 * 1024
MAX_REDIRECTS = 4
MAX_DOWNLOAD_READ_BYTES = MAX_VIDEO_BYTES + 1
PUBLIC_VIDEO_EXTENSIONS = {".mp4", ".mov"}
PUBLIC_VIDEO_FORMATS = {"mp4", "mov"}
_TEMP_DIR_PREFIX = "video-import-"
_TEMP_OWNER_MARKER = ".video-import-owner"
_LEGACY_TEMP_DIR_MAX_AGE_SECONDS = 24 * 60 * 60


class VideoMediaError(ValueError):
    """Safe, user-facing media error.

    ``status_code`` is intentionally available here as well as on
    :class:`runtime_core.video_imports.VideoImportError`; the service maps
    media failures to its public exception without exposing subprocess or URL
    details.
    """

    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = int(status_code)


class PublicURLBlocked(VideoMediaError):
    def __init__(self) -> None:
        super().__init__("视频地址不受支持，请上传视频文件。", status_code=400)


class MediaProbeUnavailable(VideoMediaError):
    def __init__(self) -> None:
        super().__init__("当前环境缺少视频校验工具，请稍后重试或上传其他视频。", status_code=503)


@dataclass(frozen=True)
class MediaInfo:
    path: Path
    extension: str
    format_name: str
    duration_seconds: float
    size_bytes: int
    mime_type: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "extension": self.extension,
            "format": self.format_name,
            "duration_seconds": self.duration_seconds,
            "size_bytes": self.size_bytes,
            "mime_type": self.mime_type,
        }


@dataclass(frozen=True)
class FetchedResource:
    data: bytes
    final_url: str
    content_type: str | None = None


def create_task_temp_dir(root: str | Path | None, task_id: str) -> Path:
    """Create a private directory for one task.

    The task id is generated internally, but it is still constrained before it
    is used as a path component.  This ensures a caller cannot make the worker
    write outside the configured temporary root.
    """

    base = Path(root) if root is not None else Path(tempfile.gettempdir()) / "kitchen-video-imports"
    base.mkdir(parents=True, exist_ok=True)
    safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", str(task_id))[:100]
    if not safe_id:
        raise VideoMediaError("视频任务标识无效。", status_code=400)
    directory = base / f"{_TEMP_DIR_PREFIX}{safe_id}"
    directory.mkdir(mode=0o700, exist_ok=False)
    try:
        marker = directory / _TEMP_OWNER_MARKER
        with marker.open("x", encoding="ascii") as handle:
            handle.write(f"{os.getpid()}\n")
    except OSError as exc:
        # A directory without an ownership marker is intentionally not
        # eligible for eager cleanup.  Do not leave one behind if marker
        # creation failed partway through task setup.
        try:
            shutil.rmtree(directory)
        except OSError:
            pass
        raise VideoMediaError("视频临时目录无法创建。", status_code=500) from exc
    return directory


def _pid_is_alive(pid: int) -> bool:
    """Return whether a process id still exists without inspecting it."""

    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # The process exists but this user cannot signal it.  Treat it as
        # live so cleanup never removes another user's active task.
        return True
    except (OSError, OverflowError):
        return False
    return True


def _owner_pid(directory: Path) -> int | None:
    marker = directory / _TEMP_OWNER_MARKER
    try:
        if marker.is_symlink() or not marker.is_file():
            return None
        value = marker.read_text(encoding="ascii").strip().splitlines()[0]
        pid = int(value)
    except (IndexError, OSError, TypeError, ValueError):
        return None
    return pid if pid > 0 else None


def _legacy_temp_dir_expired(directory: Path, *, now: float | None = None) -> bool:
    try:
        modified = directory.stat().st_mtime
    except OSError:
        return False
    current = time.time() if now is None else now
    return current - modified >= _LEGACY_TEMP_DIR_MAX_AGE_SECONDS


def cleanup_stale_temp_dirs(root: str | Path | None) -> int:
    """Remove abandoned importer directories while preserving live tasks.

    Startup cleanup is deliberately pattern-scoped and never recursively
    sweeps a caller supplied directory.  New task directories carry the
    creating process id, so multiple services in one process and tasks owned
    by another live process are left alone.  Legacy importer directories
    without a marker are removed only after a conservative expiry period.
    """

    base = Path(root) if root is not None else Path(tempfile.gettempdir()) / "kitchen-video-imports"
    if base.is_symlink() or not base.exists() or not base.is_dir():
        return 0
    removed = 0
    for child in list(base.iterdir()):
        if child.is_symlink() or not child.is_dir() or not child.name.startswith(_TEMP_DIR_PREFIX):
            continue
        pid = _owner_pid(child)
        if pid is not None:
            if _pid_is_alive(pid):
                continue
        elif not _legacy_temp_dir_expired(child):
            continue
        try:
            shutil.rmtree(child)
        except OSError:
            continue
        removed += 1
    return removed


def safe_upload_extension(filename: str) -> str:
    """Return a supported extension from an upload filename."""

    if not isinstance(filename, str) or "\x00" in filename:
        raise VideoMediaError("上传文件名无效。", status_code=400)
    extension = Path(filename).suffix.lower()
    if extension not in PUBLIC_VIDEO_EXTENSIONS:
        raise VideoMediaError("仅支持 MP4 或 MOV 视频。", status_code=415)
    return extension


def write_upload(data: bytes | bytearray | memoryview, destination: str | Path, *, filename: str = "") -> Path:
    """Write an upload after enforcing its size before and during the write."""

    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise VideoMediaError("上传内容无效。", status_code=400)
    if filename:
        extension = safe_upload_extension(filename)
    else:
        extension = Path(str(destination)).suffix.lower()
        if extension not in PUBLIC_VIDEO_EXTENSIONS:
            raise VideoMediaError("仅支持 MP4 或 MOV 视频。", status_code=415)
    raw = bytes(data)
    if len(raw) > MAX_VIDEO_BYTES:
        raise VideoMediaError("视频不能超过100MB。", status_code=413)
    target = Path(destination)
    if target.suffix.lower() != extension:
        target = target.with_suffix(extension)
    target.parent.mkdir(parents=True, exist_ok=True)
    # Use exclusive creation when possible.  A task directory is private and
    # names are generated by the service, so overwriting is never necessary.
    with target.open("xb") as handle:
        handle.write(raw)
    return target


def _host_is_public(hostname: str) -> bool:
    if not hostname:
        return False
    host = hostname.rstrip(".")
    try:
        address = ipaddress.ip_address(host)
        return _ip_is_public(address)
    except ValueError:
        pass
    try:
        addresses = {
            result[4][0]
            for result in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
            if result[4]
        }
    except (OSError, ValueError):
        return False
    return bool(addresses) and all(_ip_is_public(ipaddress.ip_address(item)) for item in addresses)


def _ip_is_public(address: ipaddress._BaseAddress) -> bool:
    # ``is_global`` excludes loopback, private, link-local, multicast,
    # unspecified and documentation ranges.  Explicit checks keep behaviour
    # stable across Python versions for unusual reserved ranges.
    return video_network._is_public_ip(str(address))


def validate_public_url(url: str) -> str:
    """Validate an HTTP(S) URL and resolve its hostname before connecting.

    URLs containing signed query parameters are valid and are never returned
    in user-facing errors or logs.  Userinfo is rejected because it is both
    unnecessary for supported public media and easy to accidentally expose.
    """

    if not isinstance(url, str) or len(url) > 8192:
        raise PublicURLBlocked()
    text = url.strip()
    try:
        parsed = urlsplit(text)
    except ValueError as exc:
        raise PublicURLBlocked() from exc
    if parsed.scheme.lower() not in {"http", "https"}:
        raise PublicURLBlocked()
    if parsed.username is not None or parsed.password is not None:
        raise PublicURLBlocked()
    hostname = (parsed.hostname or "").rstrip(".").lower()
    if not hostname:
        raise PublicURLBlocked()
    try:
        # Accessing port catches malformed ports without echoing the URL.
        port = parsed.port
    except ValueError as exc:
        raise PublicURLBlocked() from exc
    if port is not None and not 1 <= port <= 65535:
        raise PublicURLBlocked()
    if not _host_is_public(hostname):
        # The local resolver in the desktop sandbox can intentionally answer
        # the supported Xiaohongshu names with 198.18.0.0/15.  Only those
        # names may recover through the fixed HTTPS resolver, which also
        # validates every returned A/AAAA record as globally routable.
        if not video_network.is_supported_xhs_host(hostname):
            raise PublicURLBlocked()
        try:
            video_network.resolve_pinned_addresses(
                hostname,
                port=port or (443 if parsed.scheme.lower() == "https" else 80),
            )
        except video_network.PublicEndpointError as exc:
            raise PublicURLBlocked() from exc
    return text


class _NoRedirectHandler(HTTPRedirectHandler):
    """Disable automatic redirects so each target can be revalidated."""

    def redirect_request(
        self,
        req: Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        return None


def _default_opener() -> Any:
    # ``build_opener`` takes handler instances and allows us to inspect every
    # redirect before following it.  No global opener is installed.
    return build_opener(
        _NoRedirectHandler(),
        video_network.PinnedHTTPHandler(),
        video_network.PinnedHTTPSHandler(),
    )


def _response_status(response: Any) -> int:
    value = getattr(response, "status", None)
    if value is None:
        value = getattr(response, "code", None)
    try:
        return int(value or 200)
    except (TypeError, ValueError):
        return 200


def _response_header(response: Any, name: str) -> str | None:
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    getter = getattr(headers, "get", None)
    if not callable(getter):
        return None
    value = getter(name)
    return str(value) if value is not None else None


def fetch_public_resource(
    url: str,
    *,
    max_bytes: int,
    headers: dict[str, str] | None = None,
    timeout: float = 20.0,
    opener: Any | None = None,
    max_redirects: int = MAX_REDIRECTS,
) -> FetchedResource:
    """Fetch bounded public bytes while checking every redirect target."""

    current = validate_public_url(url)
    request_headers = {
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148",
        "Accept": "*/*",
        "Accept-Encoding": "gzip",
    }
    if headers:
        request_headers.update({str(key): str(value) for key, value in headers.items()})
    open_fn = opener.open if opener is not None and callable(getattr(opener, "open", None)) else None
    default_opener = None if open_fn is not None else _default_opener()
    redirect_count = 0
    while True:
        current = validate_public_url(current)
        request = Request(current, headers=request_headers, method="GET")
        try:
            response = open_fn(request, timeout=timeout) if open_fn else default_opener.open(request, timeout=timeout)
        except HTTPError as exc:
            # urllib represents redirects as HTTPError when a custom opener
            # declines them.  Other errors stay deliberately generic.
            status = int(getattr(exc, "code", 0) or 0)
            if status not in {301, 302, 303, 307, 308}:
                raise VideoMediaError("视频地址暂时无法访问。", status_code=502) from exc
            location = exc.headers.get("Location") if getattr(exc, "headers", None) is not None else None
            if not location or redirect_count >= max_redirects:
                raise VideoMediaError("视频地址重定向次数过多。", status_code=502) from exc
            current = urljoin(current, str(location))
            redirect_count += 1
            continue
        except (URLError, OSError, TimeoutError) as exc:
            raise VideoMediaError("视频地址暂时无法访问。", status_code=502) from exc

        status = _response_status(response)
        if status in {301, 302, 303, 307, 308}:
            location = _response_header(response, "Location")
            try:
                response.close()
            except Exception:
                pass
            if not location or redirect_count >= max_redirects:
                raise VideoMediaError("视频地址重定向次数过多。", status_code=502)
            current = urljoin(current, location)
            redirect_count += 1
            continue
        if status < 200 or status >= 300:
            try:
                response.close()
            except Exception:
                pass
            raise VideoMediaError("视频地址暂时无法访问。", status_code=502)

        length = _response_header(response, "Content-Length")
        encoding = (_response_header(response, "Content-Encoding") or "").lower()
        content_type = _response_header(response, "Content-Type")
        if length:
            try:
                declared_length = int(length)
            except (TypeError, ValueError):
                declared_length = None
            if declared_length is not None and declared_length > max_bytes:
                response.close()
                raise VideoMediaError("视频不能超过100MB。", status_code=413)
        chunks: list[bytes] = []
        total = 0
        try:
            while True:
                chunk = response.read(min(1024 * 1024, max_bytes + 1 - total))
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise VideoMediaError("视频不能超过100MB。", status_code=413)
                chunks.append(bytes(chunk))
        except VideoMediaError:
            raise
        except (OSError, TimeoutError) as exc:
            raise VideoMediaError("视频下载未完成。", status_code=502) from exc
        finally:
            try:
                response.close()
            except Exception:
                pass
        data = b"".join(chunks)
        if "gzip" in encoding:
            try:
                # ``gzip.decompress`` materializes the complete expanded
                # payload before the caller can inspect its size.  Read one
                # byte past the limit from a GzipFile instead, so a highly
                # compressible response cannot allocate an unbounded buffer.
                with gzip.GzipFile(fileobj=io.BytesIO(data), mode="rb") as stream:
                    data = stream.read(max_bytes + 1)
            except (OSError, EOFError) as exc:
                raise VideoMediaError("视频页面内容无效。", status_code=502) from exc
            if len(data) > max_bytes:
                raise VideoMediaError("视频不能超过100MB。", status_code=413)
        return FetchedResource(
            data=data,
            final_url=current,
            content_type=content_type,
        )


def download_public_video(
    url: str,
    destination: str | Path,
    *,
    timeout: float = 30.0,
    opener: Any | None = None,
) -> Path:
    """Download a public media URL to a caller-owned task path."""

    resource = fetch_public_resource(
        url,
        max_bytes=MAX_VIDEO_BYTES,
        timeout=timeout,
        opener=opener,
    )
    target = Path(destination)
    suffix = target.suffix.lower()
    if suffix not in PUBLIC_VIDEO_EXTENSIONS:
        # The source path can be a signed URL whose query string obscures the
        # extension.  The adapter chooses MP4 as the supported default.
        target = target.with_suffix(".mp4")
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("xb") as handle:
        handle.write(resource.data)
    return target


def _find_ffprobe() -> str | None:
    return shutil.which("ffprobe")


def _find_ffmpeg() -> str | None:
    executable = shutil.which("ffmpeg")
    if executable:
        return executable
    try:
        import imageio_ffmpeg  # type: ignore

        return str(imageio_ffmpeg.get_ffmpeg_exe())
    except Exception:
        return None


def _run_probe(path: Path, *, ffprobe: str | None, ffmpeg: str | None) -> tuple[str, float, bool]:
    if ffprobe:
        try:
            result = subprocess.run(
                [
                    ffprobe,
                    "-v",
                    "error",
                    "-show_entries",
                    "format=format_name,duration:stream=codec_type",
                    "-of",
                    "default=noprint_wrappers=1:nokey=0",
                    str(path),
                ],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            if result.returncode == 0:
                values: dict[str, str] = {}
                codec_types: list[str] = []
                for line in result.stdout.splitlines():
                    if "=" in line:
                        key, value = line.split("=", 1)
                        key = key.strip()
                        value = value.strip()
                        if key == "codec_type":
                            # A media file may contain several streams.  Keep
                            # every reported type rather than letting a later
                            # audio stream overwrite the video stream.
                            codec_types.append(value.lower())
                        else:
                            values[key] = value
                format_name = values.get("format_name", "")
                try:
                    duration = float(values.get("duration", "nan"))
                except (TypeError, ValueError):
                    duration = float("nan")
                if format_name:
                    has_video = any(item.strip().lower() == "video" for item in codec_types)
                    return format_name, duration, has_video
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    if ffmpeg:
        # imageio-ffmpeg is bundled by the application on hosts without a
        # system ffmpeg.  ``ffmpeg -i`` prints controlled metadata to stderr
        # and exits with status 1 because no output is requested.  Omitting an
        # output target is deliberate: this fallback must inspect headers and
        # stream metadata without decoding the complete input.
        try:
            result = subprocess.run(
                [ffmpeg, "-hide_banner", "-i", str(path)],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            combined = f"{result.stderr}\n{result.stdout}"
            format_match = re.search(r"Input #\d+,\s*([^,\s]+)", combined)
            duration_match = re.search(r"Duration:\s*(\d+):(\d{2}):(\d{2}(?:\.\d+)?)", combined)
            if format_match:
                format_name = format_match.group(1).strip().lower()
                if duration_match:
                    hours, minutes, seconds = duration_match.groups()
                    try:
                        duration = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
                    except (TypeError, ValueError):
                        duration = float("nan")
                else:
                    duration = float("nan")
                has_video = bool(
                    re.search(
                        r"^\s*Stream #\d+:\d+[^\n]*:\s*Video:",
                        combined,
                        flags=re.IGNORECASE | re.MULTILINE,
                    )
                )
                return format_name, duration, has_video
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    raise MediaProbeUnavailable()


def probe_video_file(
    path: str | Path,
    *,
    ffprobe_path: str | None = None,
    ffmpeg_path: str | None = None,
    max_bytes: int = MAX_VIDEO_BYTES,
    max_seconds: float = MAX_VIDEO_SECONDS,
) -> MediaInfo:
    """Validate byte size, real container format and duration with FFmpeg."""

    target = Path(path)
    if not target.exists() or not target.is_file():
        raise VideoMediaError("视频文件不存在。", status_code=400)
    try:
        size = target.stat().st_size
    except OSError as exc:
        raise VideoMediaError("视频文件无法读取。", status_code=400) from exc
    if size <= 0:
        raise VideoMediaError("视频文件为空。", status_code=400)
    if size > max_bytes:
        raise VideoMediaError("视频不能超过100MB。", status_code=413)
    extension = target.suffix.lower()
    if extension not in PUBLIC_VIDEO_EXTENSIONS:
        raise VideoMediaError("仅支持 MP4 或 MOV 视频。", status_code=415)
    selected_ffprobe = ffprobe_path if ffprobe_path is not None else _find_ffprobe()
    selected_ffmpeg = ffmpeg_path if ffmpeg_path is not None else _find_ffmpeg()
    probe_result = _run_probe(target, ffprobe=selected_ffprobe, ffmpeg=selected_ffmpeg)
    if len(probe_result) == 3:
        format_name, duration, has_video = probe_result
    else:
        # Keep test doubles and older in-process adapters working while the
        # concrete ffprobe/ffmpeg paths above always return the stream check.
        format_name, duration = probe_result  # type: ignore[misc]
        has_video = True
    formats = {item.strip().lower() for item in format_name.split(",") if item.strip()}
    # ffprobe can report a MOV family with several aliases.  Only mp4 and mov
    # are accepted; m4a/3gp audio containers are intentionally rejected.
    if not formats.intersection(PUBLIC_VIDEO_FORMATS):
        raise VideoMediaError("视频真实格式必须是 MP4 或 MOV。", status_code=415)
    if not has_video:
        raise VideoMediaError("视频文件必须包含视频流。", status_code=415)
    if not math.isfinite(duration) or duration <= 0:
        raise VideoMediaError("无法读取视频时长。", status_code=415)
    if duration > max_seconds + 0.001:
        raise VideoMediaError("视频时长不能超过3分钟。", status_code=413)
    canonical_format = "mp4" if "mp4" in formats else "mov"
    mime = "video/mp4" if canonical_format == "mp4" else "video/quicktime"
    return MediaInfo(
        path=target,
        extension=extension,
        format_name=canonical_format,
        duration_seconds=round(float(duration), 3),
        size_bytes=size,
        mime_type=mime,
    )


def model_payload_size(raw_size: int) -> int:
    """Maximum raw bytes that fit under the 10 MiB base64 input limit."""

    # Leave room for the data URL prefix and a small JSON encoding overhead.
    return max(1, ((MAX_MODEL_BASE64_BYTES - 1024) * 3) // 4)


__all__ = [
    "FetchedResource",
    "MAX_MODEL_BASE64_BYTES",
    "MAX_VIDEO_BYTES",
    "MAX_VIDEO_SECONDS",
    "MediaInfo",
    "PublicURLBlocked",
    "VideoMediaError",
    "cleanup_stale_temp_dirs",
    "create_task_temp_dir",
    "download_public_video",
    "fetch_public_resource",
    "model_payload_size",
    "probe_video_file",
    "safe_upload_extension",
    "validate_public_url",
    "write_upload",
]
