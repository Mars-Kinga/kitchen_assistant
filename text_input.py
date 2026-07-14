from __future__ import annotations

import json
import sys

from runtime_core.executor import RuntimeExecutor
from runtime_core.logger import RuntimeLogger
from runtime_core.skill_manager import SkillManager


def create_runtime() -> tuple[SkillManager, RuntimeExecutor, RuntimeLogger]:
    manager = SkillManager()
    registry = manager.load_skills()
    print(f"[启动] 已注册 {len(registry)} 个 Skill")
    for skill in registry:
        print(f"  - {skill['name']}: {skill.get('description', '')}")
    return manager, RuntimeExecutor(), RuntimeLogger()


def handle_text(user_text: str, manager: SkillManager, executor: RuntimeExecutor, logger: RuntimeLogger) -> None:
    result = manager.run_user_text(user_text)
    logger.log("user_input", {"mode": "text", "text": user_text})
    logger.log("system_result", result)

    print("\n[Agent / Skill 结果]")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    executor.execute(result)


def main() -> None:
    manager, executor, logger = create_runtime()

    if len(sys.argv) > 1:
        handle_text(" ".join(sys.argv[1:]).strip(), manager, executor, logger)
        return

    print("\n进入文字交互模式，输入 exit 退出。")
    while True:
        user_text = input("\n你：").strip()
        if user_text.lower() in {"exit", "quit", "q"}:
            break
        if not user_text:
            continue
        handle_text(user_text, manager, executor, logger)


if __name__ == "__main__":
    main()
