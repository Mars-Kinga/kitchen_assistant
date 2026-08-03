from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = ROOT / "skills" / "kitchen_assistant"
RECIPES_DIR = SKILL_ROOT / "recipes"
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

from kitchen.dish_profiles import load_catalog  # noqa: E402
from kitchen.ingredient_vocabulary import (  # noqa: E402
    ingredient_present,
    restriction_matches,
    split_main_foods_and_seasonings,
)
from kitchen.models import RecipeSearchRequest  # noqa: E402
from kitchen.recipe_normalizer import RecipeNormalizer  # noqa: E402
from providers.mock_recipe_provider import MockRecipeSearchProvider  # noqa: E402

SCRIPTS_ROOT = ROOT / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))
from prune_generated_recipe_duplicates import (  # noqa: E402
    canonical_dish_title,
    prune_generated_duplicates,
)


PINNED_REVISION = "c05758fa661ac4efa0361a987b700a351a22159b"
VAGUE_MARKERS = ("适量", "少量", "少许", "按口味")


def test_catalog_contains_exactly_one_hundred_unique_recipes() -> None:
    recipes, _ = load_catalog(RECIPES_DIR)
    curated = [recipe for recipe in recipes if recipe.get("source_key") == "howtocook"]

    assert len(recipes) == 100
    assert len(curated) == 90
    assert len({recipe["recipe_id"] for recipe in recipes}) == len(recipes)
    assert len({recipe["name"] for recipe in recipes}) == len(recipes)
    assert all(recipe["source_revision"] == PINNED_REVISION for recipe in curated)
    assert all(PINNED_REVISION in recipe["source_url"] for recipe in curated)
    assert all(recipe["source_url"].endswith(recipe["source_path"]) for recipe in curated)
    assert all(recipe["license"] == "Unlicense" for recipe in curated)


def test_curated_recipes_are_concrete_and_normalize_for_supported_servings() -> None:
    recipes, _ = load_catalog(RECIPES_DIR)
    normalizer = RecipeNormalizer()

    for recipe in recipes:
        if recipe.get("source_key") != "howtocook":
            continue
        assert all(
            not any(marker in str(item.get("amount", "")) for marker in VAGUE_MARKERS)
            for item in recipe["ingredients"]
        ), recipe["name"]
        assert all(
            not any(marker in str(step.get("instruction", "")) for marker in VAGUE_MARKERS)
            for step in recipe["steps"]
        ), recipe["name"]
        for servings in (1, 2, 3):
            normalized = normalizer.normalize(recipe, servings=servings)
            assert normalized["servings"] == servings
            assert normalized["ingredients"]
            assert normalized["steps"]


@pytest.mark.parametrize(
    ("dish", "expected_ingredient"),
    [
        ("蒜蓉西兰花", "西兰花"),
        ("黄焖鸡", "鸡腿肉"),
        ("清蒸鲈鱼", "鲈鱼"),
        ("炸酱面", "面条"),
    ],
)
def test_offline_provider_finds_curated_dishes_across_categories(
    dish: str,
    expected_ingredient: str,
) -> None:
    provider = MockRecipeSearchProvider(RECIPES_DIR)
    candidates = provider.search_recipes(RecipeSearchRequest(requested_dish=dish, servings=2))

    assert candidates
    assert candidates[0].title == dish
    assert ingredient_present(expected_ingredient, candidates[0].main_ingredients)
    assert candidates[0].source_name == "HowToCook（人工校订）"
    assert PINNED_REVISION in str(candidates[0].source_url)


def test_imported_vocabulary_handles_fresh_seafood_and_hides_cooking_water() -> None:
    assert restriction_matches("海鲜", "鲜虾")
    assert restriction_matches("海鲜", "鲜鱼")
    assert restriction_matches("蛋类", "皮蛋")

    foods, seasonings = split_main_foods_and_seasonings(
        ["鲈鱼", "葱", "清水", "八角", "生抽"]
    )
    assert "清水" not in foods
    assert "清水" not in seasonings
    assert "八角" in seasonings


def test_catalog_loader_rejects_duplicate_ids_across_files(tmp_path: Path) -> None:
    (tmp_path / "catalog").mkdir()
    base = {
        "recipes": [{"recipe_id": "same", "name": "甲"}],
        "dish_profiles": {},
    }
    extra = {"recipes": [{"recipe_id": "same", "name": "乙"}]}
    (tmp_path / "recipes.json").write_text(json.dumps(base, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "catalog" / "extra.json").write_text(
        json.dumps(extra, ensure_ascii=False),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="重复 recipe_id"):
        load_catalog(tmp_path)


def test_generated_duplicate_pruning_understands_display_variants(tmp_path: Path) -> None:
    recipes_dir = tmp_path / "recipes"
    generated_dir = recipes_dir / "generated"
    generated_dir.mkdir(parents=True)
    (recipes_dir / "recipes.json").write_text(
        json.dumps(
            {
                "recipes": [{"recipe_id": "local_kung_pao", "name": "宫保鸡丁"}],
                "dish_profiles": {},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    bundle = {
        "cache_version": 5,
        "recipes": [
            {"recipe_id": "a", "name": "新手零失败宫保鸡丁"},
            {"recipe_id": "b", "name": "香菇滑鸡"},
        ],
    }
    target = generated_dir / "cached_宫保鸡丁.json"
    target.write_text(json.dumps(bundle, ensure_ascii=False), encoding="utf-8")

    preview = prune_generated_duplicates(recipes_dir, apply=False)
    assert preview["removed"] == []
    assert len(json.loads(target.read_text(encoding="utf-8"))["recipes"]) == 2

    bundle["recipes"].append({"recipe_id": "c", "name": "宫保鸡丁"})
    target.write_text(json.dumps(bundle, ensure_ascii=False), encoding="utf-8")
    applied = prune_generated_duplicates(recipes_dir, apply=True)
    assert applied["updated_files"] == ["cached_宫保鸡丁.json"]
    assert [row["name"] for row in json.loads(target.read_text(encoding="utf-8"))["recipes"]] == [
        "新手零失败宫保鸡丁",
        "香菇滑鸡",
    ]
    assert canonical_dish_title("番茄炒蛋（少油版）") == "番茄炒蛋（少油版）"


def test_checked_in_generated_cache_has_no_exact_local_title_duplicates() -> None:
    recipes, _ = load_catalog(RECIPES_DIR)
    local_titles = {canonical_dish_title(recipe["name"]) for recipe in recipes}
    duplicates: list[str] = []

    for path in sorted((RECIPES_DIR / "generated").glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        rows = payload.get("recipes", []) if isinstance(payload, dict) else []
        if not rows and isinstance(payload, dict):
            rows = [payload]
        for row in rows:
            title = canonical_dish_title(str(row.get("name") or ""))
            if title in local_titles:
                duplicates.append(f"{path.name}:{row.get('name')}")

    assert duplicates == []
