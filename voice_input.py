"""兼容语音入口，默认进入逐轮手动录音模式。

录音、ASR 和 Runtime 主链路仍由 ``robot_main`` 与
``runtime_core.voice_io`` 维护；此文件只保留旧命令兼容性。
"""

from __future__ import annotations

import sys

from robot_main import main


def build_voice_argv(arguments: list[str]) -> list[str]:
    """Add voice/manual flags while preserving all shared CLI options."""
    result = list(arguments)
    if "--voice" not in result:
        result.insert(0, "--voice")
    if "--manual" not in result:
        result.insert(1, "--manual")
    return result


if __name__ == "__main__":
    sys.argv[1:] = build_voice_argv(sys.argv[1:])
    main()
