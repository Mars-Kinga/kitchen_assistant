from __future__ import annotations

from pathlib import Path

from .voice_io import record_wav_vad, transcribe_audio


class AudioInput:
    """兼容旧入口的单轮语音输入封装。"""

    def listen_once(self, output_path: str | Path = "voice_outputs/user.wav") -> str:
        wav_path = record_wav_vad(output_path)
        print("语音识别中...")
        text = transcribe_audio(wav_path)
        print(f"[识别结果] {text}")
        return text.strip()
