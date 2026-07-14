from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any


class RuntimeLogger:
    def __init__(self, log_dir: str | Path = "runtime_logs") -> None:
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.log_file = self.log_dir / "events.jsonl"

    def log(self, event: str, payload: dict[str, Any]) -> None:
        record = {
            "time": datetime.now().isoformat(timespec="seconds"),
            "event": event,
            "payload": payload,
        }
        with self.log_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
