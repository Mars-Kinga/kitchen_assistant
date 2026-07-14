from __future__ import annotations

import json

from runtime_core.audio_in import AudioInput
from runtime_core.executor import RuntimeExecutor
from runtime_core.logger import RuntimeLogger
from runtime_core.skill_manager import SkillManager


def main() -> None:
    manager = SkillManager()
    registry = manager.load_skills()
    executor = RuntimeExecutor()
    logger = RuntimeLogger()
    audio = AudioInput()

    print(f"[启动] 已注册 {len(registry)} 个 Skill")
    for skill in registry:
        print(f"  - {skill['name']}: {skill.get('description', '')}")

    print("\n进入语音输入模式。按 Enter 开始一次语音输入，输入 exit 退出。")
    while True:
        command = input("\n按 Enter 开始录音 / 输入 exit 退出：").strip()
        if command.lower() in {"exit", "quit", "q"}:
            break

        user_text = audio.listen_once()
        if not user_text:
            print("[语音输入] 没有识别到有效文本。")
            continue

        result = manager.run_user_text(user_text)
        logger.log("user_input", {"mode": "voice", "text": user_text})
        logger.log("system_result", result)

        print("\n[Agent / Skill 结果]")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        executor.execute(result)


if __name__ == "__main__":
    main()
