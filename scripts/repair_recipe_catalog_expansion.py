#!/usr/bin/env python3
"""Repair the already-expanded catalog against recipes/recipes.json."""

from __future__ import annotations

import json
from pathlib import Path

from expand_recipe_catalog import CATALOG, STAPLES, _recipe


def main() -> None:
    base = json.loads((CATALOG.parent / "recipes.json").read_text(encoding="utf-8"))
    base_names = {str(row["name"]) for row in base.get("recipes", [])}
    base_ids = {str(row["recipe_id"]) for row in base.get("recipes", [])}
    names: set[str] = set(base_names)
    ids: set[str] = set(base_ids)
    removed = 0
    staples_path = CATALOG / "staples_and_soups.json"
    for path in sorted(CATALOG.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        kept = []
        for row in payload["recipes"]:
            if row["name"] in base_names:
                removed += 1
                continue
            kept.append(row)
            names.add(str(row["name"]))
            ids.add(str(row["recipe_id"]))
        payload["recipes"] = kept
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    target_staples = 80
    staples_payload = json.loads(staples_path.read_text(encoding="utf-8"))
    current_local = sum(str(row.get("recipe_id", "")).startswith("local_expansion_") for row in staples_payload["recipes"])
    added = 0
    next_index = 301
    for name in STAPLES:
        if current_local + added >= target_staples:
            break
        if name in names:
            continue
        row = _recipe(name, "staples_and_soups", next_index)
        next_index += 1
        if row["recipe_id"] in ids:
            continue
        staples_payload["recipes"].append(row)
        names.add(name)
        ids.add(str(row["recipe_id"]))
        added += 1
    staples_path.write_text(json.dumps(staples_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if removed != 3 or added != 3:
        raise SystemExit(f"修复数量异常：移除 {removed}，补充 {added}")
    print(f"已修复：移除 {removed} 条基础目录重复菜名，补充 {added} 条主食菜谱。")


if __name__ == "__main__":
    main()
