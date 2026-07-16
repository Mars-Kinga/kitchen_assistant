from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import re
from typing import Any, Callable, Iterable

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

from .agent import SkillAgent
from .mock_robot_sdk import SUPPORTED_CAPABILITIES


class SkillContractError(RuntimeError):
    """Raised when a Skill manifest, input or output breaks its contract."""


class SkillManager:
    def __init__(
        self,
        skills_dir: str | Path = "skills",
        *,
        available_capabilities: Iterable[str] | None = None,
    ) -> None:
        self.base_dir = Path(__file__).resolve().parents[1]
        self.skills_dir = (self.base_dir / skills_dir).resolve()
        self.available_capabilities = frozenset(
            SUPPORTED_CAPABILITIES if available_capabilities is None else available_capabilities
        )
        self.agent = SkillAgent()
        self.registry: list[dict[str, Any]] = []
        self.load_errors: list[str] = []
        self._modules: dict[str, Any] = {}
        self.active_skill_name: str | None = None

    def load_skills(self) -> list[dict[str, Any]]:
        self.registry = []
        self.load_errors = []
        self._modules = {}
        if not self.skills_dir.exists():
            return self.registry

        registered_names: set[str] = set()
        for child in sorted(self.skills_dir.iterdir()):
            if not child.is_dir():
                continue
            manifest_path = child / "skill.json"
            if not manifest_path.exists():
                continue
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                item = self._validate_manifest(child, manifest, registered_names)
            except (OSError, json.JSONDecodeError, SkillContractError, SchemaError) as exc:
                self.load_errors.append(f"{child.name}: {exc}")
                continue
            if item is None:
                continue
            registered_names.add(item["name"])
            self.registry.append(item)
        return self.registry

    def _validate_manifest(
        self,
        skill_dir: Path,
        manifest: Any,
        registered_names: set[str],
    ) -> dict[str, Any] | None:
        if not isinstance(manifest, dict):
            raise SkillContractError("skill.json 必须是 JSON 对象")
        if manifest.get("enabled") is not True:
            return None
        required_types = {
            "name": str,
            "display_name": str,
            "version": str,
            "description": str,
            "triggers": list,
            "entrypoint": str,
            "input_schema": str,
            "output_schema": str,
            "required_services": list,
            "required_capabilities": list,
        }
        for field, expected_type in required_types.items():
            if not isinstance(manifest.get(field), expected_type):
                raise SkillContractError(f"字段 {field} 缺失或类型错误")
        name = manifest["name"].strip()
        if not name or not re.fullmatch(r"[A-Za-z0-9_-]+", name):
            raise SkillContractError("name 只能包含字母、数字、下划线和连字符")
        if name in registered_names:
            raise SkillContractError(f"Skill 名称重复：{name}")
        if not all(isinstance(item, str) and item.strip() for item in manifest["triggers"]):
            raise SkillContractError("triggers 必须是非空字符串数组")
        if not all(isinstance(item, str) and item.strip() for item in manifest["required_capabilities"]):
            raise SkillContractError("required_capabilities 必须是非空字符串数组")
        if not all(isinstance(item, str) and item.strip() for item in manifest["required_services"]):
            raise SkillContractError("required_services 必须是字符串数组")

        missing = set(manifest["required_capabilities"]) - self.available_capabilities
        if missing:
            raise SkillContractError(f"运行时缺少能力：{', '.join(sorted(missing))}")

        entrypoint = self._resolve_inside(skill_dir, manifest["entrypoint"], "entrypoint")
        if not entrypoint.is_file():
            raise SkillContractError(f"入口文件不存在：{manifest['entrypoint']}")
        input_schema_path = self._resolve_inside(skill_dir, manifest["input_schema"], "input_schema")
        output_schema_path = self._resolve_inside(skill_dir, manifest["output_schema"], "output_schema")
        input_schema = self._load_schema(input_schema_path, "input_schema")
        output_schema = self._load_schema(output_schema_path, "output_schema")

        item = dict(manifest)
        item["_skill_dir"] = str(skill_dir.resolve())
        item["_entrypoint_path"] = str(entrypoint)
        item["_input_validator"] = Draft202012Validator(input_schema)
        item["_output_validator"] = Draft202012Validator(output_schema)
        return item

    @staticmethod
    def _resolve_inside(skill_dir: Path, relative_path: str, field: str) -> Path:
        candidate = Path(relative_path)
        if candidate.is_absolute():
            raise SkillContractError(f"{field} 必须是 Skill 目录内的相对路径")
        resolved = (skill_dir / candidate).resolve()
        try:
            resolved.relative_to(skill_dir.resolve())
        except ValueError as exc:
            raise SkillContractError(f"{field} 不能离开 Skill 目录") from exc
        return resolved

    @staticmethod
    def _load_schema(path: Path, field: str) -> dict[str, Any]:
        if not path.is_file():
            raise SkillContractError(f"{field} 文件不存在：{path.name}")
        try:
            schema = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SkillContractError(f"{field} 不是有效 JSON：{exc}") from exc
        if not isinstance(schema, dict):
            raise SkillContractError(f"{field} 必须是 JSON 对象")
        Draft202012Validator.check_schema(schema)
        return schema

    def run_user_text(
        self,
        user_text: str,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        if not self.registry:
            self.load_skills()

        # Active skills own short follow-up commands such as "下一步". This is
        # deliberately process-local because this MVP has no session_id.
        if self.active_skill_name and self.get_skill(self.active_skill_name):
            route_result = {
                "route": "skill_call",
                "selected_skill": self.active_skill_name,
                "reason": "继续活动 Skill 会话。",
            }
        else:
            route_result = self.agent.select_skill(user_text, self.registry)
        if route_result.get("route") != "skill_call":
            return route_result

        skill = self.get_skill(route_result.get("selected_skill"))
        if not skill:
            return {
                "route": "normal_chat",
                "selected_skill": None,
                "reason": f"找不到 Skill：{route_result.get('selected_skill')}",
            }

        arguments = {"user_text": user_text}
        try:
            result = self.call_skill(skill, arguments, progress_callback=progress_callback)
        except Exception as exc:
            self.active_skill_name = None
            print(f"[SkillManager-警告] {skill['name']} 执行失败：{exc}")
            result = self._safe_skill_error(skill, "Skill 执行或数据校验失败，请稍后重试。")
        result["selected_skill"] = skill["name"]
        result["_route"] = route_result
        result["_arguments"] = arguments
        if result.get("session_active") is True:
            self.active_skill_name = skill["name"]
        elif skill["name"] == self.active_skill_name:
            self.active_skill_name = None
        return result

    def get_skill(self, name: str | None) -> dict[str, Any] | None:
        for skill in self.registry:
            if skill.get("name") == name:
                return skill
        return None

    def poll_active_skill(self) -> dict[str, Any] | None:
        """Return an optional local asynchronous event from the active Skill."""
        if not self.active_skill_name:
            return None
        skill = self.get_skill(self.active_skill_name)
        module = self._modules.get(self.active_skill_name)
        poll = getattr(module, "poll", None) if module is not None else None
        if skill is None or not callable(poll):
            return None
        try:
            result = poll()
            if result is None:
                return None
            self._validate_payload(skill, result, output=True)
        except Exception as exc:
            print(f"[SkillManager-警告] {skill['name']} 异步输出失败：{exc}")
            self.active_skill_name = None
            return self._safe_skill_error(skill, "Skill 计时提醒异常，当前任务已安全结束。")
        result["selected_skill"] = skill["name"]
        result["_route"] = {
            "route": "skill_call",
            "selected_skill": skill["name"],
            "reason": "活动 Skill 的本地计时提醒。",
        }
        if result.get("session_active") is not True:
            self.active_skill_name = None
        return result

    def call_skill(
        self,
        skill: dict[str, Any],
        arguments: dict[str, Any],
        *,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        self._validate_payload(skill, arguments, output=False)
        entrypoint = Path(skill["_entrypoint_path"])
        module = self._modules.get(skill["name"])
        if module is None:
            spec = importlib.util.spec_from_file_location(f"{skill['name']}_run", entrypoint)
            if spec is None or spec.loader is None:
                raise RuntimeError(f"无法加载 Skill 入口：{entrypoint}")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            self._modules[skill["name"]] = module
        if not hasattr(module, "run"):
            raise RuntimeError(f"Skill 入口缺少 run(arguments) 函数：{entrypoint}")

        set_progress_callback = getattr(module, "set_progress_callback", None)
        if progress_callback is None or not callable(set_progress_callback):
            result = module.run(arguments)
            self._validate_payload(skill, result, output=True)
            return result
        set_progress_callback(progress_callback)
        try:
            result = module.run(arguments)
            self._validate_payload(skill, result, output=True)
            return result
        finally:
            # Callbacks belong to one host turn only.  Keeping one around
            # would make a later timer event write into a stale executor.
            set_progress_callback(None)

    @staticmethod
    def _validate_payload(skill: dict[str, Any], payload: Any, *, output: bool) -> None:
        validator_key = "_output_validator" if output else "_input_validator"
        label = "输出" if output else "输入"
        validator = skill.get(validator_key)
        if not isinstance(validator, Draft202012Validator):
            raise SkillContractError(f"Skill {label}校验器未加载")
        try:
            validator.validate(payload)
        except ValidationError as exc:
            location = ".".join(str(item) for item in exc.absolute_path) or "根节点"
            raise SkillContractError(f"Skill {label}不符合 Schema（{location}）：{exc.message}") from exc

    @staticmethod
    def _safe_skill_error(skill: dict[str, Any], message: str) -> dict[str, Any]:
        return {
            "route": "skill_result",
            "task_name": str(skill.get("display_name") or skill.get("name") or "Skill"),
            "session_active": False,
            "speech": message,
            "display": "Skill 暂时不可用｜请稍后重试",
            "robot_action": "show_concern",
            "led_effect": "yellow",
            "expression": "alert",
            "error_code": "SKILL_CONTRACT_OR_EXECUTION_ERROR",
        }
