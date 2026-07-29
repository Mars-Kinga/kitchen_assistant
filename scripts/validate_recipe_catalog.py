#!/usr/bin/env python3
"""Validate the local recipe catalog without calling any online service."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = ROOT / "skills" / "kitchen_assistant"
RECIPES_DIR = SKILL_ROOT / "recipes"
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

from kitchen.dish_profiles import load_catalog  # noqa: E402
from kitchen.recipe_normalizer import RecipeNormalizer  # noqa: E402


VAGUE_MARKERS = ("适量", "少量", "少许", "按口味")
MINIMUM_CURATED_RECIPES = 20
EXPECTED_TOTAL_RECIPES = 100


def validate_catalog(recipes_dir: Path = RECIPES_DIR) -> list[str]:
    errors: list[str] = []
    recipes, _ = load_catalog(recipes_dir)
    manifest = _load_manifest(recipes_dir / "sources.json", errors)
    sources = manifest.get("sources", {}) if isinstance(manifest, dict) else {}
    curated = [recipe for recipe in recipes if recipe.get("source_key")]

    if len(recipes) != EXPECTED_TOTAL_RECIPES:
        errors.append(
            f"本地菜谱总数为 {len(recipes)}，要求恰好为 {EXPECTED_TOTAL_RECIPES}"
        )
    if len(curated) < MINIMUM_CURATED_RECIPES:
        errors.append(
            f"人工校订菜谱只有 {len(curated)} 道，至少需要 {MINIMUM_CURATED_RECIPES} 道"
        )

    normalizer = RecipeNormalizer()
    for recipe in curated:
        name = str(recipe.get("name") or recipe.get("recipe_id") or "未命名菜谱")
        source_key = str(recipe.get("source_key") or "")
        source = sources.get(source_key) if isinstance(sources, dict) else None
        if not isinstance(source, dict):
            errors.append(f"{name}: source_key={source_key!r} 未在 sources.json 中声明")
            continue
        revision = str(source.get("revision") or "")
        if recipe.get("source_revision") != revision:
            errors.append(f"{name}: source_revision 与来源清单不一致")
        if recipe.get("license") != source.get("license"):
            errors.append(f"{name}: license 与来源清单不一致")
        source_url = str(recipe.get("source_url") or "")
        source_path = str(recipe.get("source_path") or "")
        if not source_url or revision not in source_url:
            errors.append(f"{name}: source_url 必须指向固定 revision")
        if not source_path or not source_url.endswith(source_path):
            errors.append(f"{name}: source_url 与 source_path 不一致")

        ingredients = recipe.get("ingredients")
        if not isinstance(ingredients, list) or not ingredients:
            errors.append(f"{name}: ingredients 不能为空")
        else:
            for index, item in enumerate(ingredients, start=1):
                if not isinstance(item, dict):
                    errors.append(f"{name}: 第 {index} 个食材不是对象")
                    continue
                amount = str(item.get("amount") or "").strip()
                if not str(item.get("name") or "").strip() or not amount:
                    errors.append(f"{name}: 第 {index} 个食材缺少 name 或 amount")
                if any(marker in amount for marker in VAGUE_MARKERS):
                    errors.append(f"{name}: 第 {index} 个食材包含模糊用量 {amount!r}")

        steps = recipe.get("steps")
        if not isinstance(steps, list) or not steps:
            errors.append(f"{name}: steps 不能为空")
        else:
            for index, step in enumerate(steps, start=1):
                instruction = str(step.get("instruction") if isinstance(step, dict) else "")
                if any(marker in instruction for marker in VAGUE_MARKERS):
                    errors.append(f"{name}: 第 {index} 步包含模糊用量")

        for servings in (1, 2, 3):
            try:
                normalized = normalizer.normalize(recipe, servings=servings)
            except Exception as exc:  # Keep the CLI useful by reporting every bad recipe.
                errors.append(f"{name}: {servings} 人份标准化失败：{type(exc).__name__}: {exc}")
                continue
            if normalized.get("servings") != servings:
                errors.append(f"{name}: {servings} 人份标准化结果人数不一致")
    return errors


def _load_manifest(path: Path, errors: list[str]) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"无法读取来源清单 {path}: {exc}")
        return {}
    if not isinstance(payload, dict) or not isinstance(payload.get("sources"), dict):
        errors.append(f"来源清单格式错误：{path}")
        return {}
    return payload


def main() -> int:
    errors = validate_catalog()
    if errors:
        print(f"菜谱目录校验失败（{len(errors)} 项）：", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    recipes, _ = load_catalog(RECIPES_DIR)
    curated_count = sum(bool(recipe.get("source_key")) for recipe in recipes)
    print(f"菜谱目录校验通过：共 {len(recipes)} 道，其中人工校订导入 {curated_count} 道。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
