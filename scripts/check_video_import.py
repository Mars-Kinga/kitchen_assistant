"""Opt-in real video import probe; never confirms or starts a kitchen task."""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from runtime_core.video_imports import VideoImportService  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="验证真实视频提取，输出待用户确认的草稿，不启动教学")
    parser.add_argument("share_text", help="小红书分享链接或完整文案")
    parser.add_argument("--output", type=Path, help="可选：将最终草稿写入本地 JSON 文件")
    parser.add_argument("--upload", action="store_true", help="将位置参数作为本地 MP4/MOV 文件路径")
    args = parser.parse_args()
    service = VideoImportService()
    started = time.monotonic()
    try:
        if args.upload:
            path = Path(args.share_text)
            snapshot = service.submit_upload(path.name, path.read_bytes())
        else:
            snapshot = service.submit_link(args.share_text)
        last_stage = None
        while time.monotonic() - started < 330:
            if snapshot["stage"] != last_stage:
                print(snapshot["stage"], snapshot.get("message", ""), flush=True)
                last_stage = snapshot["stage"]
            if snapshot["stage"] in {"review", "confirmed", "failed", "cancelled"}:
                break
            time.sleep(1)
            snapshot = service.get(snapshot["id"])
        else:
            service.cancel(snapshot["id"])
            print("验证超时；任务已取消。", file=sys.stderr)
            return 1
        if snapshot["stage"] != "review":
            print(snapshot.get("error") or snapshot.get("message") or "导入未成功", file=sys.stderr)
            return 1
        draft = snapshot["draft"]
        print(json.dumps({
            "name": draft.get("name"), "servings": draft.get("servings"),
            "ingredients": draft.get("ingredients"), "steps": draft.get("steps"),
            "import_metadata": draft.get("import_metadata"),
            "metrics": snapshot.get("metrics"),
            "elapsed_seconds": round(time.monotonic() - started, 1),
        }, ensure_ascii=False, indent=2))
        if args.output:
            args.output.write_text(json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"待确认草稿已写入：{args.output.resolve()}")
        print("验证完成；菜谱仍需用户在控制台确认。")
        return 0
    except Exception as exc:
        from runtime_core.video_imports import VideoImportError

        if isinstance(exc, (VideoImportError, ValueError)):
            print(str(exc), file=sys.stderr)
        else:
            print("验证失败，请检查视频导入配置。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
