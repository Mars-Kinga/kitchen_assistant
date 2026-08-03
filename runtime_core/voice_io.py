from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections import deque
from pathlib import Path
from typing import Any


API_KEY = os.getenv("MIMO_API_KEY") or os.getenv("TOKEN_PLAN_API_KEY", "")
BASE_URL = os.getenv("MIMO_BASE_URL", "https://token-plan-cn.xiaomimimo.com/v1").rstrip("/")
ASR_MODEL = os.getenv("MIMO_ASR_MODEL", "mimo-v2.5")

_last_spoken_text: str | None = None
_last_spoken_time = 0.0
SUPPRESS_DUPLICATE_SECONDS = 1.0


def rms_level(block: Any) -> float:
    import numpy as np

    data = block.astype(np.float32)
    if data.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(data * data)) / 32768.0)


def calibrate_noise(
    samplerate: int,
    block_ms: int,
    device: str | int | None,
    seconds: float,
) -> float:
    import numpy as np
    import sounddevice as sd

    block_frames = int(samplerate * block_ms / 1000)
    levels: list[float] = []
    print(f"校准环境噪声 {seconds:.1f} 秒，请先不要说话...")
    with sd.InputStream(
        samplerate=samplerate,
        channels=1,
        dtype="int16",
        blocksize=block_frames,
        device=device,
    ) as stream:
        deadline = time.time() + seconds
        while time.time() < deadline:
            block, _ = stream.read(block_frames)
            levels.append(rms_level(block))
    noise = float(np.median(levels)) if levels else 0.002
    print(f"环境噪声 RMS: {noise:.4f}")
    return noise


def record_wav_vad(
    path: str | Path,
    samplerate: int = 16000,
    device: str | int | None = None,
    block_ms: int = 30,
    start_threshold: float | None = None,
    end_threshold: float | None = None,
    silence_ms: int = 900,
    pre_roll_ms: int = 300,
    min_record_ms: int = 500,
    max_record_seconds: float = 20,
    calibration_seconds: float = 0.8,
) -> Path:
    import numpy as np
    import sounddevice as sd
    from scipy.io import wavfile

    path = Path(path)
    block_frames = int(samplerate * block_ms / 1000)
    silence_blocks = max(1, int(silence_ms / block_ms))
    pre_roll_blocks = max(0, int(pre_roll_ms / block_ms))
    min_blocks = max(1, int(min_record_ms / block_ms))
    max_blocks = max(1, int(max_record_seconds * 1000 / block_ms))

    if start_threshold is None or end_threshold is None:
        noise = calibrate_noise(samplerate, block_ms, device, calibration_seconds)
        dynamic_start = max(0.010, noise * 4.0)
        dynamic_end = max(0.006, noise * 2.5)
        start_threshold = dynamic_start if start_threshold is None else start_threshold
        end_threshold = dynamic_end if end_threshold is None else end_threshold

    print(
        f"正在监听：说话自动开始，停止说话约 {silence_ms}ms 后自动截断。"
        f" start={start_threshold:.4f}, end={end_threshold:.4f}"
    )

    pre_roll: deque[np.ndarray] = deque(maxlen=pre_roll_blocks)
    recorded: list[np.ndarray] = []
    triggered = False
    quiet_count = 0
    total_blocks = 0

    with sd.InputStream(
        samplerate=samplerate,
        channels=1,
        dtype="int16",
        blocksize=block_frames,
        device=device,
    ) as stream:
        while True:
            block, _ = stream.read(block_frames)
            level = rms_level(block)

            if not triggered:
                pre_roll.append(block.copy())
                print(f"监听中 RMS={level:.4f}    ", end="\r")
                if level >= start_threshold:
                    triggered = True
                    recorded.extend(list(pre_roll))
                    recorded.append(block.copy())
                    total_blocks = len(recorded)
                    quiet_count = 0
                    print("\n检测到说话，开始录音...")
                continue

            recorded.append(block.copy())
            total_blocks += 1
            quiet_count = quiet_count + 1 if level < end_threshold else 0
            print(f"录音中 RMS={level:.4f} 静音块={quiet_count}/{silence_blocks}    ", end="\r")

            long_enough = total_blocks >= min_blocks
            silence_done = quiet_count >= silence_blocks
            too_long = total_blocks >= max_blocks
            if (long_enough and silence_done) or too_long:
                print()
                break

    if not recorded:
        raise RuntimeError("没有录到音频")

    audio = np.concatenate(recorded, axis=0)
    path.parent.mkdir(parents=True, exist_ok=True)
    wavfile.write(str(path), samplerate, audio)
    seconds = len(audio) / samplerate
    print(f"录音保存: {path} ({seconds:.1f}s)")
    return path


def audio_data_url(audio_path: str | Path) -> str:
    path = Path(audio_path)
    mime = mimetypes.guess_type(path.name)[0] or "audio/wav"
    data = base64.b64encode(path.read_bytes()).decode("utf-8")
    return f"data:{mime};base64,{data}"


def _curl_chat(payload: dict[str, Any], timeout: int = 60) -> dict[str, Any]:
    if not API_KEY:
        raise RuntimeError('缺少 MIMO_API_KEY，请先设置：$env:MIMO_API_KEY="你的 tp-... key"')

    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", delete=False) as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
        tmp = f.name
    try:
        proc = subprocess.run(
            [
                "curl.exe" if sys.platform == "win32" else "curl",
                "--http1.1",
                "--connect-timeout",
                "30",
                "--max-time",
                str(timeout),
                "--retry",
                "3",
                "--retry-all-errors",
                "--silent",
                "--show-error",
                "--fail-with-body",
                "--location",
                f"{BASE_URL}/chat/completions",
                "--header",
                f"api-key: {API_KEY}",
                "--header",
                "Content-Type: application/json",
                "--data",
                f"@{tmp}",
            ],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=timeout + 30,
        )
    finally:
        Path(tmp).unlink(missing_ok=True)

    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip())
    return json.loads(proc.stdout)


def transcribe_audio(audio_path: str | Path) -> str:
    payload = {
        "model": ASR_MODEL,
        "messages": [
            {"role": "system", "content": "你是一个语音识别助手。请准确转写用户音频。"},
            {
                "role": "user",
                "content": [
                    {"type": "input_audio", "input_audio": {"data": audio_data_url(audio_path)}},
                    {"type": "text", "text": "请把音频转写为文本。只输出转写文本，不要解释。"},
                ],
            },
        ],
        "max_completion_tokens": 128,
        "thinking": {"type": "disabled"},
    }
    result = _curl_chat(payload)
    message = result["choices"][0]["message"]
    return (message.get("content") or message.get("reasoning_content") or "").strip()


class VoiceOutput:
    def __init__(
        self,
        tts_backend: str = "edge",
        edge_voice: str = "zh-CN-XiaoxiaoNeural",
        edge_rate: str = "+8%",
        no_play: bool = False,
    ) -> None:
        self.tts_backend = tts_backend
        self.edge_voice = edge_voice
        self.edge_rate = edge_rate
        self.no_play = no_play

    def speak(self, text: str) -> None:
        if not text:
            return

        global _last_spoken_text, _last_spoken_time
        now = time.time()
        if text == _last_spoken_text and (now - _last_spoken_time) < SUPPRESS_DUPLICATE_SECONDS:
            return
        _last_spoken_text = text
        _last_spoken_time = now

        print(f"[语音] {text}")
        if self.no_play:
            return

        if self.tts_backend == "pyttsx3":
            self._speak_pyttsx3(text)
        else:
            self._speak_edge(text)

    def _speak_edge(self, text: str) -> None:
        try:
            import edge_tts
        except Exception:
            print("[TTS] 缺少 edge-tts，降级到 pyttsx3")
            self._speak_pyttsx3(text)
            return

        try:
            tmp = Path(tempfile.mktemp(suffix=".mp3"))
            asyncio.run(self._edge_save(edge_tts, text, tmp))
            self._play_audio(tmp)
            tmp.unlink(missing_ok=True)
        except Exception as exc:
            print(f"[TTS] edge-tts 失败，降级到 pyttsx3: {exc}")
            self._speak_pyttsx3(text)

    async def _edge_save(self, edge_tts: Any, text: str, path: Path) -> None:
        communicate = edge_tts.Communicate(text, self.edge_voice, rate=self.edge_rate)
        await communicate.save(str(path))

    def _speak_pyttsx3(self, text: str) -> None:
        try:
            import pyttsx3
        except Exception:
            print("[TTS] 缺少 pyttsx3，跳过语音播放")
            return

        try:
            engine = pyttsx3.init()
            for voice in engine.getProperty("voices"):
                info = f"{voice.id} {voice.name}".lower()
                if "chinese" in info or "zh" in info or "huihui" in info:
                    engine.setProperty("voice", voice.id)
                    break
            engine.setProperty("rate", 190)
            engine.say(text)
            engine.runAndWait()
        except Exception as exc:
            print(f"[TTS] pyttsx3 失败: {exc}")

    @staticmethod
    def _play_audio(path: Path) -> None:
        path = path.resolve()
        if path.suffix.lower() == ".wav":
            try:
                import winsound

                winsound.PlaySound(str(path), winsound.SND_FILENAME)
                return
            except Exception:
                pass

        # macOS already ships an MP3-capable player. Prefer it over pygame so
        # the text/TTS demo never requires local SDL headers or a C build.
        if sys.platform == "darwin" and shutil.which("afplay"):
            try:
                subprocess.run(["afplay", str(path)], check=True)
                return
            except (OSError, subprocess.CalledProcessError) as exc:
                raise RuntimeError(f"系统 afplay 播放失败：{exc}") from exc

        if path.suffix.lower() == ".mp3":
            try:
                import pygame

                pygame.mixer.init()
                pygame.mixer.music.load(str(path))
                pygame.mixer.music.play()
                while pygame.mixer.music.get_busy():
                    time.sleep(0.05)
                pygame.mixer.music.unload()
                return
            except Exception as exc:
                raise RuntimeError(
                    "当前系统缺少可用的 MP3 播放器；可使用 --no-play 跳过播放"
                ) from exc

        subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                f"(New-Object Media.SoundPlayer '{path}').PlaySync()",
            ],
            check=False,
        )
