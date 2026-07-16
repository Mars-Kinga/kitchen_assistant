from __future__ import annotations

import json
from pathlib import Path

import pytest

from runtime_core.skill_manager import SkillContractError, SkillManager


CAPABILITIES = ["voice", "display", "motion", "light", "expression"]


def _write_skill(root: Path, name: str, run_source: str, *, capabilities: list[str] | None = None) -> None:
    skill_dir = root / name
    (skill_dir / "scripts").mkdir(parents=True)
    (skill_dir / "schemas").mkdir()
    manifest = {
        "name": name,
        "display_name": name,
        "version": "1.0.0",
        "description": "测试 Skill",
        "triggers": [name],
        "negative_triggers": [],
        "entrypoint": "scripts/run.py",
        "input_schema": "schemas/input.json",
        "output_schema": "schemas/output.json",
        "required_services": [],
        "required_capabilities": capabilities or CAPABILITIES,
        "enabled": True,
    }
    (skill_dir / "skill.json").write_text(json.dumps(manifest), encoding="utf-8")
    (skill_dir / "schemas" / "input.json").write_text(json.dumps({
        "type": "object",
        "required": ["user_text"],
        "properties": {"user_text": {"type": "string", "minLength": 1}},
    }), encoding="utf-8")
    (skill_dir / "schemas" / "output.json").write_text(json.dumps({
        "type": "object",
        "required": ["route", "task_name", "speech", "display", "robot_action", "led_effect", "expression"],
        "properties": {"route": {"const": "skill_result"}},
    }), encoding="utf-8")
    (skill_dir / "scripts" / "run.py").write_text(run_source, encoding="utf-8")


def test_project_manifests_and_schemas_load_cleanly() -> None:
    manager = SkillManager()

    registry = manager.load_skills()

    assert {skill["name"] for skill in registry} == {"hello_skill", "kitchen_assistant"}
    assert manager.load_errors == []


def test_unavailable_capability_disables_only_affected_skills() -> None:
    manager = SkillManager(available_capabilities={"voice"})

    assert manager.load_skills() == []
    assert len(manager.load_errors) == 2
    assert all("运行时缺少能力" in error for error in manager.load_errors)


def test_bad_manifest_is_isolated_from_valid_skill(tmp_path: Path) -> None:
    (tmp_path / "broken").mkdir()
    (tmp_path / "broken" / "skill.json").write_text("{not-json", encoding="utf-8")
    _write_skill(tmp_path, "working", """
def run(arguments):
    return {
        'route': 'skill_result', 'task_name': 'working', 'speech': 'ok',
        'display': 'ok', 'robot_action': 'nod', 'led_effect': 'white',
        'expression': 'neutral'
    }
""")
    manager = SkillManager(tmp_path)

    registry = manager.load_skills()

    assert [skill["name"] for skill in registry] == ["working"]
    assert len(manager.load_errors) == 1
    assert manager.run_user_text("working")["speech"] == "ok"


def test_invalid_skill_output_becomes_safe_five_channel_feedback(tmp_path: Path) -> None:
    _write_skill(tmp_path, "invalid", """
def run(arguments):
    return {'route': 'skill_result', 'task_name': 'invalid', 'speech': 'missing channels'}
""")
    manager = SkillManager(tmp_path)
    manager.load_skills()

    response = manager.run_user_text("invalid")

    assert response["error_code"] == "SKILL_CONTRACT_OR_EXECUTION_ERROR"
    assert response["session_active"] is False
    for key in ("speech", "display", "robot_action", "led_effect", "expression"):
        assert response[key]


def test_skill_input_schema_is_enforced() -> None:
    manager = SkillManager()
    manager.load_skills()
    hello = manager.get_skill("hello_skill")
    assert hello is not None

    with pytest.raises(SkillContractError, match="输入不符合 Schema"):
        manager.call_skill(hello, {"user_text": ""})
