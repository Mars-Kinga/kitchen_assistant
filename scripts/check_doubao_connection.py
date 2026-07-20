"""Make one explicit real request through the production Doubao client."""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = ROOT / "skills" / "kitchen_assistant"
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

from llm.doubao_client import DoubaoClientError, DoubaoLLMClient  # noqa: E402


def main() -> int:
    client = DoubaoLLMClient()
    if not client.is_available():
        print("错误：未设置 ARK_API_KEY 环境变量。", file=sys.stderr)
        return 1
    try:
        content = client.chat([
            {
                "role": "system",
                "content": "你是谨慎、简洁的厨房助手，只回答当前问题。",
            },
            {
                "role": "user",
                "content": "我用小锅煮面，面条太长放不进去。先软化一端再慢慢压进去，可以吗？",
            },
        ])
    except DoubaoClientError as exc:
        print(f"豆包调用失败：{exc}", file=sys.stderr)
        return 2
    print(content)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
