from __future__ import annotations

import errno
import json
import mimetypes
import socket
import sys
import threading
import time
from email import policy
from email.parser import BytesParser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from .executor import RuntimeExecutor
from .ingredient_vision import IngredientVisionService
from .skill_manager import SkillManager


MAX_REQUEST_BYTES = 12 * 1024 * 1024
ASSET_ROOT = Path(__file__).resolve().parent / "kitchen_console_static"
DEFAULT_CONSOLE_PORT = 18765


class _ConsoleHTTPServer(ThreadingHTTPServer):
    # SO_REUSEADDR on Windows can permit two processes to bind the same port.
    allow_reuse_address = sys.platform != "win32"
    allow_reuse_port = False


class KitchenConsoleService:
    """Bridge the LAN console to the existing runtime without hardware control."""

    def __init__(
        self,
        manager: SkillManager,
        executor: RuntimeExecutor,
        vision_service: IngredientVisionService,
        *,
        video_service: Any | None = None,
    ) -> None:
        self.manager = manager
        self.executor = executor
        self.vision_service = vision_service
        self.started_at = time.time()
        self._video_service = video_service
        self._video_lock = threading.Lock()

    @property
    def videos(self) -> Any:
        with self._video_lock:
            if self._video_service is None:
                from .video_imports import VideoImportService

                self._video_service = VideoImportService()
            return self._video_service

    def status(self) -> dict[str, Any]:
        try:
            kitchen = self.manager.call_skill_hook(
                "kitchen_assistant",
                "status_snapshot",
            )
        except Exception:
            kitchen = {
                "state": "IDLE",
                "session_active": False,
                "recipe": None,
                "current_step": None,
                "timer": None,
                "candidates": [],
                "confirmed_ingredients": [],
                "provider_mode": "mock",
            }
        return {
            "server_time": time.time(),
            "uptime_seconds": max(0, round(time.time() - self.started_at)),
            "robot": self.executor.status_snapshot(),
            "camera": self.vision_service.status_snapshot(),
            "kitchen": kitchen,
            "timers": [kitchen["timer"]] if kitchen.get("timer") else [],
            "scope_notice": (
                "本控制台用于食材识别、菜谱建议和流程状态展示，"
                "不提供食品或设备安全判断，也不提供事故处置方案。"
            ),
        }

    def capture_camera(self) -> dict[str, Any]:
        return {
            "image_data_url": self.vision_service.capture_preview(),
            "camera": self.vision_service.status_snapshot(),
        }

    def analyze_image(self, image_data_url: str) -> dict[str, Any]:
        return self.vision_service.analyze_pantry_image(image_data_url)

    def recommend(
        self,
        ingredients: list[str],
        *,
        servings: int,
        taste: str,
    ) -> dict[str, Any]:
        result = self.manager.call_skill_hook(
            "kitchen_assistant",
            "recommend_from_ingredients",
            ingredients,
            servings=servings,
            taste=taste,
            validate_output=True,
            activate_session=True,
        )
        self._execute_console_feedback("生成菜谱方案", result)
        return _recommendation_response(result)

    def browse_recipes(self, query: str = "", *, category: str = "", limit: int = 500) -> dict[str, Any]:
        recipes = self.manager.call_skill_hook(
            "kitchen_assistant",
            "browse_local_recipes",
            query,
            category=category,
            limit=limit,
        )
        return {"recipes": recipes, "query": query, "category": category, "count": len(recipes)}

    def recipe_categories(self) -> list[dict[str, Any]]:
        return self.manager.call_skill_hook("kitchen_assistant", "browse_recipe_categories")

    def generate_recipe(self, dish_name: str, *, servings: int = 1) -> dict[str, Any]:
        result = self.manager.call_skill_hook(
            "kitchen_assistant",
            "generate_console_recipe",
            dish_name,
            servings=servings,
            validate_output=True,
            activate_session=True,
        )
        self._execute_console_feedback("确认 AI 生成菜谱", result)
        return _recommendation_response(result)

    def recipe_detail(self, recipe_id: str, *, servings: int = 1) -> dict[str, Any]:
        if recipe_id.startswith("video_"):
            return self.videos.recipe_detail(recipe_id, servings=servings)
        return self.manager.call_skill_hook(
            "kitchen_assistant",
            "console_recipe_detail",
            recipe_id,
            servings=servings,
        )

    def select_recipe(self, recipe_id: str, *, servings: int = 1) -> dict[str, Any]:
        if recipe_id.startswith("video_"):
            recipe = self.videos.recipe_detail(recipe_id, servings=servings)
            result = self.manager.call_skill_hook(
                "kitchen_assistant", "select_imported_console_recipe", recipe,
                validate_output=True, activate_session=True,
            )
        else:
            result = self.manager.call_skill_hook(
                "kitchen_assistant",
                "select_console_recipe",
                recipe_id,
                servings=servings,
                validate_output=True,
                activate_session=True,
            )
        self._execute_console_feedback("选择菜谱", result)
        return {
            "kitchen_state": result.get("kitchen_state"),
            "message": _feedback_text(result),
            "status": self.status()["kitchen"],
        }

    def navigate_step(self, direction: str) -> dict[str, Any]:
        result = self.manager.call_skill_hook(
            "kitchen_assistant",
            "navigate_console_step",
            direction,
        )
        label = "上一步" if direction == "previous" else "下一步"
        self._execute_console_feedback(label, result)
        return {
            "message": _feedback_text(result),
            "status": self.status()["kitchen"],
        }

    def cooking_action(self, action: str) -> dict[str, Any]:
        result = self.manager.call_skill_hook(
            "kitchen_assistant", "console_cooking_action", action,
            validate_output=True, activate_session=True,
        )
        self._execute_console_feedback("烹饪操作", result)
        return {"message": _feedback_text(result), "status": self.status()["kitchen"]}

    def _execute_console_feedback(self, action: str, result: dict[str, Any]) -> None:
        """Make LAN controls visible through the normal simulated robot path."""
        print(f"\n[局域网控制台] {action}")
        print(f"[机器人回复] {_speech_text(result)}")
        self.executor.execute_plan(result)


class KitchenConsoleServer:
    def __init__(
        self,
        service: KitchenConsoleService,
        *,
        host: str = "0.0.0.0",
        port: int = DEFAULT_CONSOLE_PORT,
        port_attempts: int = 20,
    ) -> None:
        if not 0 <= port <= 65535:
            raise ValueError("控制台端口必须在 0–65535 之间")
        if not 1 <= port_attempts <= 100:
            raise ValueError("端口尝试次数必须在 1–100 之间")
        handler = _handler_factory(service)
        # Bind directly so there is no race between checking and reserving a port.
        last_port = port if port == 0 else min(65535, port + port_attempts - 1)
        for candidate in range(port, last_port + 1):
            try:
                self.httpd = _ConsoleHTTPServer((host, candidate), handler)
                break
            except OSError as exc:
                occupied = exc.errno == errno.EADDRINUSE or getattr(exc, "winerror", None) == 10048
                # Windows can report access denied for an excluded/reserved port.
                reserved = exc.errno == errno.EACCES and sys.platform == "win32"
                if not (occupied or reserved) or candidate == last_port:
                    raise
        self.httpd.daemon_threads = True
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return int(self.httpd.server_address[1])

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self.httpd.serve_forever,
            name="kitchen-lan-console",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

    def access_urls(self) -> list[str]:
        addresses = {"127.0.0.1"}
        try:
            for item in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                address = item[4][0]
                if address and not address.startswith("127."):
                    addresses.add(address)
        except OSError:
            pass
        return [f"http://{address}:{self.port}" for address in sorted(addresses)]


def _handler_factory(service: KitchenConsoleService) -> type[BaseHTTPRequestHandler]:
    class KitchenConsoleHandler(BaseHTTPRequestHandler):
        server_version = "KitchenConsole/1.0"

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/api/imported-recipes" or parsed.path.startswith("/api/video-imports/"):
                try:
                    if parsed.path == "/api/imported-recipes":
                        recipes = service.videos.list_recipes()
                        self._send_json(HTTPStatus.OK, {"recipes": recipes, "count": len(recipes)})
                    else:
                        self._send_json(HTTPStatus.OK, service.videos.get(self._video_id(parsed.path)))
                except Exception as exc:
                    self._send_api_exception(exc)
                return
            if parsed.path == "/api/status":
                self._send_json(HTTPStatus.OK, service.status())
                return
            if parsed.path == "/api/recipes":
                query = parse_qs(parsed.query).get("q", [""])[0][:60]
                category = parse_qs(parsed.query).get("category", [""])[0][:40]
                self._send_json(HTTPStatus.OK, service.browse_recipes(query, category=category))
                return
            if parsed.path == "/api/recipe-categories":
                self._send_json(HTTPStatus.OK, {"categories": service.recipe_categories()})
                return
            if parsed.path.startswith("/api/recipes/"):
                recipe_id = unquote(parsed.path.removeprefix("/api/recipes/"))
                servings_raw = parse_qs(parsed.query).get("servings", ["1"])[0]
                try:
                    self._send_json(
                        HTTPStatus.OK,
                        service.recipe_detail(recipe_id, servings=int(servings_raw)),
                    )
                except (KeyError, ValueError) as exc:
                    self._send_error(HTTPStatus.NOT_FOUND, str(exc))
                except Exception as exc:
                    self._send_api_exception(exc)
                return
            self._send_asset(parsed.path)

        def do_POST(self) -> None:  # noqa: N802
            if not self._same_origin_request():
                self._send_error(HTTPStatus.FORBIDDEN, "请求来源不受信任。")
                return
            parsed = urlparse(self.path)
            try:
                if parsed.path == "/api/video-imports":
                    if self.headers.get("Content-Type", "").startswith("multipart/form-data"):
                        filename, data = self._read_video_upload()
                        snapshot = service.videos.submit_upload(filename, data)
                    else:
                        payload = self._read_json()
                        snapshot = service.videos.submit_link(str(payload.get("share_text") or ""))
                    self._send_json(HTTPStatus.ACCEPTED, snapshot)
                    return
                if parsed.path.startswith("/api/video-imports/") and parsed.path.endswith("/confirm"):
                    self._send_json(HTTPStatus.OK, service.videos.confirm(self._video_id(parsed.path, "confirm")))
                    return
                if parsed.path.startswith("/api/video-imports/") and parsed.path.endswith("/complete"):
                    self._send_json(HTTPStatus.OK, service.videos.complete_draft(self._video_id(parsed.path, "complete")))
                    return
                if parsed.path == "/api/camera/capture":
                    self._send_json(HTTPStatus.OK, service.capture_camera())
                    return
                payload = self._read_json()
                if parsed.path == "/api/ingredients/analyze":
                    self._send_json(
                        HTTPStatus.OK,
                        service.analyze_image(str(payload.get("image_data_url") or "")),
                    )
                    return
                if parsed.path == "/api/recommendations":
                    ingredients = payload.get("ingredients")
                    if not isinstance(ingredients, list) or not all(
                        isinstance(item, str) for item in ingredients
                    ):
                        raise ValueError("ingredients 必须是字符串数组。")
                    servings = int(payload.get("servings", 1))
                    taste = str(payload.get("taste") or "正常")
                    self._send_json(
                        HTTPStatus.OK,
                        service.recommend(
                            ingredients,
                            servings=servings,
                            taste=taste,
                        ),
                    )
                    return
                if parsed.path == "/api/recipes/select":
                    recipe_id = str(payload.get("recipe_id") or "").strip()
                    if not recipe_id:
                        raise ValueError("recipe_id 不能为空。")
                    self._send_json(
                        HTTPStatus.OK,
                        service.select_recipe(
                            recipe_id,
                            servings=int(payload.get("servings", 1)),
                        ),
                    )
                    return
                if parsed.path == "/api/recipes/generate":
                    dish_name = str(payload.get("dish_name") or "").strip()
                    if not dish_name:
                        raise ValueError("dish_name 不能为空。")
                    self._send_json(
                        HTTPStatus.OK,
                        service.generate_recipe(
                            dish_name,
                            servings=int(payload.get("servings", 1)),
                        ),
                    )
                    return
                if parsed.path == "/api/cooking/step":
                    direction = str(payload.get("direction") or "").strip()
                    self._send_json(HTTPStatus.OK, service.navigate_step(direction))
                    return
                if parsed.path == "/api/cooking/action":
                    self._send_json(HTTPStatus.OK, service.cooking_action(str(payload.get("action") or "")))
                    return
                self._send_error(HTTPStatus.NOT_FOUND, "接口不存在。")
            except ValueError as exc:
                self._send_api_exception(exc)
            except RuntimeError as exc:
                self._send_api_exception(exc)
            except Exception as exc:
                self._send_api_exception(exc)

        def do_PATCH(self) -> None:  # noqa: N802
            if not self._same_origin_request():
                self._send_error(HTTPStatus.FORBIDDEN, "请求来源不受信任。")
                return
            try:
                task_id = self._video_id(urlparse(self.path).path, "draft")
                self._send_json(HTTPStatus.OK, service.videos.update_draft(task_id, self._read_json()))
            except Exception as exc:
                self._send_api_exception(exc)

        def do_DELETE(self) -> None:  # noqa: N802
            if not self._same_origin_request():
                self._send_error(HTTPStatus.FORBIDDEN, "请求来源不受信任。")
                return
            try:
                self._send_json(HTTPStatus.OK, service.videos.cancel(self._video_id(urlparse(self.path).path)))
            except Exception as exc:
                self._send_api_exception(exc)

        @staticmethod
        def _video_id(path: str, suffix: str = "") -> str:
            parts = path.strip("/").split("/")
            expected = 4 if suffix else 3
            if len(parts) != expected or parts[:2] != ["api", "video-imports"] or (suffix and parts[-1] != suffix):
                raise KeyError("接口不存在。")
            return unquote(parts[2])

        def _send_api_exception(self, exc: Exception) -> None:
            status = getattr(exc, "status_code", None)
            if status is not None:
                self._send_error(HTTPStatus(status), str(exc))
            elif isinstance(exc, KeyError):
                self._send_error(HTTPStatus.NOT_FOUND, "任务或菜谱不存在。")
            elif isinstance(exc, ValueError):
                self._send_error(HTTPStatus.BAD_REQUEST, str(exc))
            elif isinstance(exc, RuntimeError):
                self._send_error(HTTPStatus.SERVICE_UNAVAILABLE, str(exc))
            else:
                self._send_error(HTTPStatus.INTERNAL_SERVER_ERROR, "请求处理失败，请稍后重试。")

        def _read_video_upload(self) -> tuple[str, bytes]:
            from .video_media import MAX_VIDEO_BYTES

            try:
                size = int(self.headers.get("Content-Length", "0"))
            except ValueError as exc:
                raise ValueError("Content-Length 无效。") from exc
            if size <= 0 or size > MAX_VIDEO_BYTES + 65536:
                raise ValueError("视频为空或超过 100 MB。")
            self.connection.settimeout(30)
            body = self.rfile.read(size)
            if len(body) != size:
                raise ValueError("视频上传不完整，请重试。")
            content_type = self.headers.get("Content-Type", "")
            header = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("utf-8")
            message = BytesParser(policy=policy.default).parsebytes(header + body)
            if not message.is_multipart():
                raise ValueError("视频上传格式无效。")
            files = [part for part in message.iter_parts() if part.get_param("name", header="content-disposition") == "file"]
            if len(files) != 1 or not files[0].get_filename():
                raise ValueError("请上传一个视频文件。")
            data = files[0].get_payload(decode=True)
            if not data or len(data) > MAX_VIDEO_BYTES:
                raise ValueError("视频为空或超过 100 MB。")
            return files[0].get_filename(), data

        def _read_json(self) -> dict[str, Any]:
            try:
                size = int(self.headers.get("Content-Length", "0"))
            except ValueError as exc:
                raise ValueError("Content-Length 无效。") from exc
            if size <= 0 or size > MAX_REQUEST_BYTES:
                raise ValueError("请求内容为空或超过 12 MB。")
            if "application/json" not in self.headers.get("Content-Type", ""):
                raise ValueError("请求必须使用 application/json。")
            try:
                payload = json.loads(self.rfile.read(size).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("JSON 内容无效。") from exc
            if not isinstance(payload, dict):
                raise ValueError("JSON 根节点必须是对象。")
            return payload

        def _same_origin_request(self) -> bool:
            origin = self.headers.get("Origin")
            if not origin:
                return True
            parsed = urlparse(origin)
            return parsed.netloc.casefold() == self.headers.get("Host", "").casefold()

        def _send_asset(self, request_path: str) -> None:
            relative = "index.html" if request_path in {"", "/"} else request_path.lstrip("/")
            target = (ASSET_ROOT / relative).resolve()
            try:
                target.relative_to(ASSET_ROOT.resolve())
            except ValueError:
                self._send_error(HTTPStatus.NOT_FOUND, "页面不存在。")
                return
            if not target.is_file():
                self._send_error(HTTPStatus.NOT_FOUND, "页面不存在。")
                return
            content = target.read_bytes()
            mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            self.send_response(HTTPStatus.OK)
            self._security_headers()
            self.send_header("Content-Type", f"{mime}; charset=utf-8" if mime.startswith("text/") else mime)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(content)

        def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
            content = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self._security_headers()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(content)

        def _send_error(self, status: HTTPStatus, message: str) -> None:
            self._send_json(status, {"error": message[:240]})

        def _security_headers(self) -> None:
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'",
            )

        def log_message(self, format: str, *args: Any) -> None:
            return

    return KitchenConsoleHandler


def _recommendation_response(result: dict[str, Any]) -> dict[str, Any]:
    candidates = result.get("recipe_candidates", [])
    error = result.get("recommendation_error")
    # A synchronous recommendation response is terminal, never still searching.
    steps = result.get("steps") or []
    final_feedback = steps[-1] if steps else result
    return {
        "candidates": candidates,
        "provider_mode": result.get("provider_mode", "mock"),
        "message": error["message"] if error else _feedback_text(final_feedback),
        "kitchen_state": result.get("kitchen_state"),
        "recommendation_status": "ready" if candidates else "failed",
        "recommendation_error": error,
    }


def _feedback_text(result: dict[str, Any]) -> str:
    if isinstance(result.get("display"), str):
        return result["display"]
    steps = result.get("steps")
    if isinstance(steps, list):
        displays = [str(item.get("display")) for item in steps if isinstance(item, dict) and item.get("display")]
        return "\n".join(displays)
    return "菜谱方案已生成。"


def _speech_text(result: dict[str, Any]) -> str:
    direct = result.get("speech") or result.get("question")
    if isinstance(direct, str) and direct:
        return direct
    steps = result.get("steps")
    if isinstance(steps, list):
        speeches = [
            str(item.get("speech") or item.get("question"))
            for item in steps
            if isinstance(item, dict) and (item.get("speech") or item.get("question"))
        ]
        if speeches:
            return " ".join(speeches)
    return _feedback_text(result)
