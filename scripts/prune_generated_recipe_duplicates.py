#!/usr/bin/env python3
"""Remove generated recipe variants already covered by the fixed local catalog."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = ROOT / "skills" / "kitchen_assistant"
RECIPES_DIR = SKILL_ROOT / "recipes"
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

from kitchen.dish_profiles import load_catalog  # noqa: E402


def canonical_dish_title(value: str) -> str:
    """Normalize harmless whitespace while preserving meaningful variants."""
    title = re.sub(r"\s+", "", str(value or ""))
    return title.strip("-—_")


def prune_generated_duplicates(
    recipes_dir: Path = RECIPES_DIR,
    *,
    apply: bool = False,
) -> dict[str, Any]:
    local_recipes, _ = load_catalog(recipes_dir)
    local_titles = {
        canonical_dish_title(str(recipe.get("name") or ""))
        for recipe in local_recipes
        if canonical_dish_title(str(recipe.get("name") or ""))
    }
    generated_dir = recipes_dir / "generated"
    removed: list[dict[str, str]] = []
    updated_files: list[str] = []
    deleted_files: list[str] = []

    if not generated_dir.exists():
        return {
            "removed": removed,
            "updated_files": updated_files,
            "deleted_files": deleted_files,
        }

    for path in sorted(generated_dir.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and isinstance(payload.get("recipes"), list):
            rows = [row for row in payload["recipes"] if isinstance(row, dict)]
            kept = []
            for row in rows:
                name = str(row.get("name") or "")
                if canonical_dish_title(name) in local_titles:
                    removed.append({"file": path.name, "name": name})
                else:
                    kept.append(row)
            if len(kept) == len(rows):
                continue
            if not kept:
                deleted_files.append(path.name)
                if apply:
                    path.unlink()
                continue
            updated_files.append(path.name)
            if apply:
                new_payload = dict(payload)
                new_payload["recipes"] = kept
                temporary = path.with_suffix(".json.tmp")
                temporary.write_text(
                    json.dumps(new_payload, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                temporary.replace(path)
            continue

        if isinstance(payload, dict):
            name = str(payload.get("name") or "")
            if canonical_dish_title(name) in local_titles:
                removed.append({"file": path.name, "name": name})
                deleted_files.append(path.name)
                if apply:
                    path.unlink()

    return {
        "removed": removed,
        "updated_files": updated_files,
        "deleted_files": deleted_files,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="实际修改 generated/；省略时只预览。",
    )
    args = parser.parse_args()
    result = prune_generated_duplicates(apply=args.apply)
    action = "已删除" if args.apply else "将删除"
    print(f"{action} {len(result['removed'])} 个重复缓存候选：")
    for item in result["removed"]:
        print(f"- {item['name']}（{item['file']}）")
    if result["deleted_files"]:
        print(f"空缓存文件：{', '.join(result['deleted_files'])}")
    if not args.apply:
        print("这是预览；传入 --apply 才会写入。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
