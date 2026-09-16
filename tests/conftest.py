"""Keep cloud-client tests independent of a developer's local credentials."""
import sys
from pathlib import Path
import pytest

SKILL_ROOT = Path(__file__).resolve().parents[1] / "skills" / "kitchen_assistant"
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

@pytest.fixture(autouse=True)
def isolate_local_qwen_config(monkeypatch, tmp_path):
    from llm import config
    monkeypatch.setattr(config, "LOCAL_CONFIG_PATH", tmp_path / "absent-qwen.env")
