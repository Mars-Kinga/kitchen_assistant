from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

from .agent import SkillAgent


class SkillManager:
    def __init__(self, skills_dir: str | Path = "skills") -> None:
        self.base_dir = Path(__file__).resolve().parents[1]
        self.skills_dir = (self.base_dir / skills_dir).resolve()
        self.agent = SkillAgent()
        self.registry: list[dict[str, Any]] = []
        self._modules: dict[str, Any] = {}
        self.active_skill_name: str | None = None

    def load_skills(self) -> list[dict[str, Any]]:
        self.registry = []
        if not self.skills_dir.exists():
            return self.registry

        for child in sorted(self.skills_dir.iterdir()):
            if not child.is_dir():
                continue
            manifest_path = child / "skill.json"
            if not manifest_path.exists():
                continue
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest.get("enabled") is not True:
                continue
            item = dict(manifest)
            item["_skill_dir"] = str(child)
            item["_entrypoint_path"] = str(child / manifest["entrypoint"])
            self.registry.append(item)
        return self.registry

    def run_user_text(self, user_text: str) -> dict[str, Any]:
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
        result = self.call_skill(skill, arguments)
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
        result = poll()
        if not isinstance(result, dict):
            return None
        result["selected_skill"] = skill["name"]
        result["_route"] = {
            "route": "skill_call",
            "selected_skill": skill["name"],
            "reason": "活动 Skill 的本地计时提醒。",
        }
        if result.get("session_active") is not True:
            self.active_skill_name = None
        return result

    def call_skill(self, skill: dict[str, Any], arguments: dict[str, Any]) -> dict[str, Any]:
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

        return module.run(arguments)
