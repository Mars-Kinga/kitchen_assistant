from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import time
from pathlib import Path

from chat_handler import handle_chat
from runtime_core.executor import RuntimeExecutor
from runtime_core.ingredient_vision import IngredientVisionService, is_visual_identification_request
from runtime_core.logger import RuntimeLogger
from runtime_core.mac_camera import MacCamera
from runtime_core.skill_manager import SkillManager
from runtime_core.voice_io import record_wav_vad, transcribe_audio
from skills.kitchen_assistant.llm.qwen_client import QwenLLMClient


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


_EXIT_PHRASES = frozenset({
    "quit", "exit", "退出", "再见", "拜拜", "拜了", "bye", "goodbye",
    "结束吧", "结束程序", "关闭程序", "不聊了", "下次见", "我先走了",
})


def is_exit_command(text: str) -> bool:
    """Recognize common typed and ASR-transcribed ways to end the runtime."""
    normalized = re.sub(r"[\s，。！？、,.!?]", "", str(text or "").casefold())
    return normalized in _EXIT_PHRASES or normalized.startswith("退出")


def create_runtime(
    args: argparse.Namespace,
) -> tuple[SkillManager, RuntimeExecutor, RuntimeLogger, IngredientVisionService]:
    manager = SkillManager()
    registry = manager.load_skills()
    print(f"[启动] 已注册 {len(registry)} 个 Skill")
    for skill in registry:
        print(f"  - {skill['name']}: {skill.get('description', '')}")
    for error in manager.load_errors:
        print(f"[Skill 加载警告] {error}")

    executor = RuntimeExecutor(
        no_play=args.no_play,
        tts_backend=args.tts_backend,
        edge_voice=args.edge_voice,
        edge_rate=args.edge_rate,
    )
    vision_service = IngredientVisionService(MacCamera(), QwenLLMClient())
    return manager, executor, RuntimeLogger(), vision_service


def handle_input(
    user_text: str,
    manager: SkillManager,
    executor: RuntimeExecutor,
    logger: RuntimeLogger,
    vision_service: IngredientVisionService | None = None,
) -> dict | None:
    if not user_text:
        return None

    if vision_service is not None and is_visual_identification_request(user_text):
        print("\n[视觉识别] 正在从 Mac 摄像头拍照并识别食材……")
        result = vision_service.recognize(user_text)
        logger.log("user_input", {"text": user_text})
        logger.log("vision_result", result)
        print("\n[用户输入]")
        print(user_text)
        print("\n[系统结果]")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        executor.execute_plan(result)
        return result

    def render_progress(progress: dict) -> None:
        logger.log("skill_progress", progress)
        print("\n[处理中]")
        executor.execute_plan(progress)

    result = manager.run_user_text(user_text, progress_callback=render_progress)
    logger.log("user_input", {"text": user_text})
    logger.log("system_result", result)

    print("\n[用户输入]")
    print(user_text)
    print("\n[系统结果]")
    print(json.dumps(result, ensure_ascii=False, indent=2))

    if result.get("route") == "normal_chat":
        chat_result = handle_chat(user_text)
        logger.log("normal_chat", chat_result)
        print("\n[普通聊天]")
        print(json.dumps(chat_result, ensure_ascii=False, indent=2))
        executor.execute_plan(chat_result)
    else:
        executor.execute_plan(result)

    return result


def voice_loop_once(
    idx: int,
    out_dir: Path,
    manager: SkillManager,
    executor: RuntimeExecutor,
    logger: RuntimeLogger,
    vision_service: IngredientVisionService,
    args: argparse.Namespace,
) -> bool | None:
    input_wav = out_dir / f"user_{idx:03d}.wav"
    try:
        record_wav_vad(
            input_wav,
            samplerate=args.samplerate,
            device=args.device,
            start_threshold=args.start_threshold,
            end_threshold=args.end_threshold,
            silence_ms=args.silence_ms,
            max_record_seconds=args.max_record_seconds,
            calibration_seconds=args.calibration_seconds,
        )
    except RuntimeError as exc:
        print(f"[录音失败] {exc}，本轮跳过。")
        return False

    print("语音识别中...")
    t0 = time.perf_counter()
    try:
        user_text = transcribe_audio(input_wav)
    except Exception as exc:
        print(f"[语音识别失败] {exc}")
        return False

    print(f"你说: {user_text}")
    print(f"ASR 耗时: {time.perf_counter() - t0:.1f}s")

    if not user_text:
        print("没有识别到有效文本，本轮跳过。")
        return False

    if is_exit_command(user_text):
        print("已退出。")
        return None

    handle_input(user_text, manager, executor, logger, vision_service)
    return True


def run_text_loop(
    manager: SkillManager,
    executor: RuntimeExecutor,
    logger: RuntimeLogger,
    vision_service: IngredientVisionService,
) -> None:
    print("\n进入文字交互模式。输入 quit、退出或再见结束。")
    stop_notifier = threading.Event()

    def notify_due_timers() -> None:
        while not stop_notifier.wait(0.25):
            result = manager.poll_active_skill()
            if result is None:
                continue
            logger.log("skill_timer_event", result)
            print("\n[计时提醒]")
            print(json.dumps(result, ensure_ascii=False, indent=2))
            executor.execute_plan(result)

    notifier = threading.Thread(target=notify_due_timers, name="kitchen-timer-notifier", daemon=True)
    notifier.start()
    try:
        while True:
            user_text = input("\n请输入用户文字：").strip()
            if is_exit_command(user_text):
                print("已退出。")
                break
            handle_input(user_text, manager, executor, logger, vision_service)
    finally:
        stop_notifier.set()
        notifier.join(timeout=1.0)


def run_voice_loop(
    manager: SkillManager,
    executor: RuntimeExecutor,
    logger: RuntimeLogger,
    vision_service: IngredientVisionService,
    args: argparse.Namespace,
) -> None:
    out_dir = Path("voice_outputs")
    out_dir.mkdir(parents=True, exist_ok=True)
    idx = 1

    if args.manual:
        print("\n语音手动模式：每轮按 Enter 开始录音。按 Ctrl+C 退出。")
    else:
        print("\n语音自动模式：持续监听麦克风，说话自动开始。按 Ctrl+C 退出。")
        print("说完后停顿一下，连续静音会自动截断本轮录音。")

    try:
        while True:
            if args.manual:
                try:
                    cmd = input(f"\n第 {idx} 轮，按 Enter 开始录音，q 退出：").strip().lower()
                except EOFError:
                    break
                if cmd in {"q", "quit", "exit"}:
                    break
            else:
                print(f"\n第 {idx} 轮：等待你说话...")

            outcome = voice_loop_once(
                idx,
                out_dir,
                manager,
                executor,
                logger,
                vision_service,
                args,
            )
            if outcome is None:
                break
            if outcome:
                idx += 1
    except KeyboardInterrupt:
        print("\n收到 Ctrl+C，正在退出。")


def main() -> None:
    parser = argparse.ArgumentParser(description="skill-runtime：文字 + 语音双模式交互")
    parser.add_argument("user_text", nargs="*", help="单次执行的文本，可选")
    parser.add_argument("--voice", action="store_true", help="语音自动监听模式")
    parser.add_argument("--manual", action="store_true", help="语音手动模式，按 Enter 开始录音")
    parser.add_argument("--no-play", action="store_true", help="不播放语音回复，只打印文本")
    parser.add_argument("--tts-backend", choices=["edge", "pyttsx3"], default="edge", help="TTS 后端")
    parser.add_argument("--edge-voice", default="zh-CN-XiaoxiaoNeural", help="edge-tts 音色")
    parser.add_argument("--edge-rate", default="+8%", help="edge-tts 语速")
    parser.add_argument("--device", default=None, help="sounddevice 输入设备编号或名称")
    parser.add_argument("--samplerate", type=int, default=16000, help="录音采样率")
    parser.add_argument("--start-threshold", type=float, default=None, help="开始说话 RMS 阈值")
    parser.add_argument("--end-threshold", type=float, default=None, help="结束说话 RMS 阈值")
    parser.add_argument("--silence-ms", type=int, default=900, help="连续静音多少 ms 后截断录音")
    parser.add_argument("--max-record-seconds", type=float, default=20, help="单轮最长录音秒数")
    parser.add_argument("--calibration-seconds", type=float, default=0.8, help="环境噪声校准秒数")
    parser.add_argument("--list-devices", action="store_true", help="列出音频设备后退出")
    args = parser.parse_args()

    if args.list_devices:
        try:
            import sounddevice as sd

            print(sd.query_devices())
        except Exception as exc:
            print(f"无法列出音频设备：{exc}")
        return

    manager, executor, logger, vision_service = create_runtime(args)

    one_shot = " ".join(args.user_text).strip()
    if one_shot:
        if is_exit_command(one_shot):
            print("已退出。")
            return
        handle_input(one_shot, manager, executor, logger, vision_service)
        return

    if args.voice:
        run_voice_loop(manager, executor, logger, vision_service, args)
    else:
        run_text_loop(manager, executor, logger, vision_service)


if __name__ == "__main__":
    main()
