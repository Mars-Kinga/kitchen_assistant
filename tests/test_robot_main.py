import subprocess
import sys
from pathlib import Path

from robot_main import is_exit_command
from voice_input import build_voice_argv


ROOT = Path(__file__).resolve().parents[1]


def test_exit_command_accepts_common_typed_and_asr_variants() -> None:
    for text in ("再见", "拜拜", "我先走了", "退出程序", "Goodbye", " bye！"):
        assert is_exit_command(text)
    assert not is_exit_command("取消计时")


def test_text_compatibility_entrypoint_delegates_to_primary_runtime() -> None:
    completed = subprocess.run(
        [sys.executable, "text_input.py", "--no-play", "你好"],
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=True,
    )

    assert '"selected_skill": "hello_skill"' in completed.stdout
    assert "[模拟SDK-动作] 挥手" in completed.stdout


def test_voice_compatibility_entrypoint_preserves_shared_options() -> None:
    assert build_voice_argv(["--no-play", "--device", "1"]) == [
        "--voice", "--manual", "--no-play", "--device", "1",
    ]
