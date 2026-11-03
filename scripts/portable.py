"""Standard-library-only installer, launcher and offline diagnostics."""
from __future__ import annotations

import argparse
import importlib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
ENV_ROOT = ROOT / ".venv"


def environment_python() -> Path:
    return ENV_ROOT / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def validate_python() -> None:
    if not (3, 11) <= sys.version_info[:2] <= (3, 14):
        raise ValueError("请安装 Python 3.11–3.14，推荐 Python 3.13（64 位）。")
    if struct.calcsize("P") != 8:
        raise ValueError("请安装 64 位 Python。Windows ARM 电脑建议使用 x64 Python 的兼容模式。")


def create_config() -> None:
    for name in ("console.json", "qwen.env"):
        target = ROOT / "config" / name
        if not target.exists():
            shutil.copyfile(target.with_name(name + ".example"), target)


def install() -> int:
    validate_python()
    python = environment_python()
    if not python.exists():
        print("正在创建本机专用运行环境……", flush=True)
        subprocess.run([sys.executable, "-m", "venv", str(ENV_ROOT)], check=True, cwd=ROOT)
    # Never copy another computer's .venv: its interpreter and paths are host-specific.
    subprocess.run([str(python), "-m", "pip", "install", "--disable-pip-version-check",
                    "-r", str(ROOT / "requirements.txt")], check=True, cwd=ROOT)
    create_config()
    result = subprocess.run([str(python), str(Path(__file__).resolve()), "check"], cwd=ROOT)
    if result.returncode == 0:
        print("\n安装完成。Windows 双击 start.cmd；Mac/Linux 运行 bash start.sh。")
    return result.returncode


def read_console_config() -> dict:
    path = ROOT / "config" / "console.json"
    if not path.exists():
        path = path.with_name("console.json.example")
    config = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(config, dict):
        raise ValueError("config/console.json 必须是 JSON 对象。")
    host = config.get("host", "0.0.0.0")
    port = config.get("port", 18765)
    attempts = config.get("port_attempts", 20)
    if not isinstance(host, str) or not host.strip():
        raise ValueError("host 必须是有效的监听地址。")
    if type(port) is not int or not 0 <= port <= 65535:
        raise ValueError("port 必须是 0–65535 的整数。")
    if type(attempts) is not int or not 1 <= attempts <= 100:
        raise ValueError("port_attempts 必须是 1–100 的整数。")
    return {"host": host, "port": port, "port_attempts": attempts}


def start(*, text_mode: bool = False) -> int:
    if not environment_python().exists():
        raise ValueError("还未安装。请先双击 install.cmd，或运行 bash install.sh。")
    create_config()
    config = read_console_config()
    argv = [str(environment_python()), str(ROOT / "robot_main.py"), "--no-play",
            "--console" if text_mode else "--console-only", "--console-open-browser",
            "--console-host", config["host"], "--console-port", str(config["port"]),
            "--console-port-attempts", str(config["port_attempts"])]
    return subprocess.call(argv, cwd=ROOT)


def check() -> int:
    validate_python()
    sys.path.insert(0, str(ROOT))
    print(f"Python：{sys.version.split()[0]} / 64 位 / {sys.platform}")
    for module in ("openai", "jsonschema", "cv2", "imageio_ffmpeg"):
        importlib.import_module(module)
        print(f"依赖正常：{module}")
    import imageio_ffmpeg
    result = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-version"],
                            capture_output=True, timeout=15)
    if result.returncode:
        raise ValueError("视频工具 FFmpeg 无法运行。请重新执行安装。")
    print("视频工具 FFmpeg：正常")
    from runtime_core.skill_manager import SkillManager
    manager = SkillManager()
    registry = manager.load_skills()
    if manager.load_errors or not any(item["name"] == "kitchen_assistant" for item in registry):
        raise ValueError(f"Skill 加载失败：{manager.load_errors}")
    print(f"Skill：成功加载 {len(registry)} 个")
    config = read_console_config()
    print(f"控制台首选端口：{config['port']}；最多尝试 {config['port_attempts']} 个端口")
    from skills.kitchen_assistant.llm.config import QwenConfig
    print("AI 密钥：" + ("已配置（未发起网络请求）" if QwenConfig.from_environment().api_key else "未配置，可使用离线菜谱"))
    print("检查通过。相机硬件、局域网连通性和 AI 权限需在目标电脑实际使用时验证。")
    return 0


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("install", "start", "check"))
    parser.add_argument("--text", action="store_true")
    args = parser.parse_args()
    os.chdir(ROOT)
    try:
        if args.action == "install":
            return install()
        if args.action == "check":
            return check()
        return start(text_mode=args.text)
    except (ValueError, OSError, ImportError, subprocess.SubprocessError) as exc:
        print(f"\n操作失败：{exc}")
        return 1
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
