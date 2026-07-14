from __future__ import annotations

import re
from copy import deepcopy
from typing import Any

from .feedback_mapping import enrich_step


_HEAT_ACTIONS = (
    "加热", "烧开", "沸腾", "煮", "炖", "焖", "煎", "烤", "蒸",
    "炸", "熬", "焯", "炒", "收汁", "微波", "电饭煲",
)
_STIR_FRY_MEAT_WORDS = ("猪肉", "猪里脊", "肉丝", "鸡肉", "牛肉丝")
_STIR_FRY_VEGETABLE_WORDS = (
    "蔬菜", "木耳", "胡萝卜", "青椒", "笋", "土豆", "茄子", "西兰花", "青菜", "洋葱",
)


class RecipeNormalizationError(ValueError):
    pass


class RecipeNormalizer:
    """Convert provider-specific dictionaries into the runtime Recipe shape."""

    def normalize(self, raw: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise RecipeNormalizationError("菜谱不是对象")
        name = self._clean(raw.get("name"))
        if not name:
            raise RecipeNormalizationError("菜名不能为空")
        ingredients = self._ingredients(raw.get("ingredients"))
        if not ingredients:
            raise RecipeNormalizationError("菜谱必须包含食材")
        raw_steps = raw.get("steps")
        if not isinstance(raw_steps, list) or not raw_steps:
            raise RecipeNormalizationError("菜谱必须包含至少一个步骤")
        prepared_steps: list[dict[str, Any]] = []
        for index, step in enumerate(raw_steps, start=1):
            instruction = self._clean(step.get("instruction") if isinstance(step, dict) else "")
            if not instruction:
                raise RecipeNormalizationError(f"第 {index} 步缺少说明")
            instruction = self._remove_thaw_clauses(instruction)
            if not instruction:
                continue
            normalized_step = {**step, "instruction": instruction}
            for expanded_step in self._split_preparation_step(normalized_step):
                expanded_instruction = str(expanded_step["instruction"])
                duration = self._timer_duration(expanded_step, expanded_instruction)
                expanded_instruction, duration, safety_note = self._enforce_realistic_timing(
                    expanded_instruction,
                    duration,
                    expanded_step.get("safety_note"),
                )
                expanded_step["instruction"] = expanded_instruction
                expanded_step["duration_seconds"] = duration
                expanded_step["safety_note"] = safety_note
                if "解冻" in str(expanded_step.get("safety_note") or ""):
                    expanded_step["safety_note"] = None
                prepared_steps.append(expanded_step)
        if not prepared_steps:
            raise RecipeNormalizationError("菜谱去除重复解冻说明后没有可执行步骤")
        cleaned_steps = [
            enrich_step(step, index, len(prepared_steps))
            for index, step in enumerate(prepared_steps, start=1)
        ]
        return {
            "recipe_id": self._clean(raw.get("recipe_id")) or f"normalized_{name}",
            "name": name,
            "source_name": self._clean(raw.get("source_name")) or "本地示例菜谱",
            "source_url": raw.get("source_url") or None,
            "servings": raw.get("servings") or raw.get("default_servings"),
            "ingredients": ingredients,
            "equipment": list(raw.get("equipment") or []),
            "estimated_time_minutes": raw.get("estimated_time_minutes") or raw.get("estimated_minutes"),
            "difficulty": self._clean(raw.get("difficulty")) or "简单",
            "notes": [self._clean(note) for note in raw.get("notes", []) if self._clean(note)],
            "steps": cleaned_steps,
        }

    def _ingredients(self, ingredients: Any) -> list[dict[str, Any]]:
        if not isinstance(ingredients, list):
            return []
        result = []
        for item in ingredients:
            if isinstance(item, str):
                name, amount = self._clean(item), "适量"
            elif isinstance(item, dict):
                name, amount = self._clean(item.get("name")), self._clean(item.get("amount")) or "适量"
            else:
                continue
            if name:
                unit = self._clean(item.get("unit")) if isinstance(item, dict) else ""
                result.append({"name": name, "amount": amount, "unit": unit or None, "optional": bool(isinstance(item, dict) and item.get("optional"))})
        return result

    @staticmethod
    def _clean(value: Any) -> str:
        text = str(value or "").strip()
        if "广告" in text or "点击" in text and "http" in text:
            return ""
        return " ".join(text.split())

    @staticmethod
    def _remove_thaw_clauses(instruction: str) -> str:
        """Remove thaw work because KitchenSession confirms it before cooking."""
        parts = [part.strip() for part in re.split(r"[，,；;。]", instruction) if part.strip()]
        kept: list[str] = []
        thaw_words = ("解冻", "冷冻肉", "冷冻肥牛", "温水化冻")
        heat_words = ("煮", "炖", "焖", "煎", "炒", "烤", "蒸", "炸", "焯", "加热")
        for part in parts:
            if not any(word in part for word in thaw_words):
                kept.append(part)
                continue
            # Preserve a cooking action after the already-confirmed thaw work,
            # e.g. “肥牛解冻后放入锅中翻炒” -> “放入锅中翻炒”.
            tail = re.split(r"后|再", part, maxsplit=1)
            if len(tail) == 2 and any(word in tail[1] for word in heat_words):
                kept.append(tail[1].lstrip("，,然后接着 "))
        return "，".join(kept).strip("，。； ")

    @staticmethod
    def _split_preparation_step(step: dict[str, Any]) -> list[dict[str, Any]]:
        """Split dense preparation prose into a few memorable user steps."""
        instruction = str(step.get("instruction", "")).strip()
        if any(action in instruction for action in _HEAT_ACTIONS):
            return [step]
        parts = [part.strip() for part in re.split(r"[，,；;。]", instruction) if part.strip()]
        if len(parts) < 4:
            return [step]

        groups: list[list[str]] = []
        current: list[str] = []
        current_kind = "prep"

        def flush() -> None:
            nonlocal current
            if current:
                groups.append(current)
                current = []

        for part in parts:
            if "泡发" in part:
                flush()
                groups.append([part])
                current_kind = "prep"
                continue
            if "腌制" in part:
                current.append(part)
                flush()
                current_kind = "prep"
                continue
            kind = "cut" if any(action in part for action in ("切丝", "切碎", "切丁", "切片", "切块")) else "prep"
            if current and kind != current_kind:
                flush()
            current.append(part)
            current_kind = kind
        flush()
        if len(groups) <= 1:
            return [step]
        return [
            {
                **step,
                "instruction": "，".join(group),
                "duration_seconds": None,
            }
            for group in groups
        ]

    @staticmethod
    def _enforce_realistic_timing(
        instruction: str,
        duration: int | float | None,
        safety_note: Any,
    ) -> tuple[str, int | float | None, str | None]:
        """Clamp obviously short AI timers and add observable doneness checks."""
        note = str(safety_note or "").strip() or None
        if duration is None or "炒" not in instruction:
            return instruction, duration, note
        if any(word in instruction for word in _STIR_FRY_MEAT_WORDS):
            duration = max(duration, 120)
            if not any(marker in instruction for marker in ("无粉红", "完全变色", "熟透")):
                instruction = f"{instruction}，炒到肉类完全变色、中心无粉红"
            reminder = "计时只是下限参考，应以肉类完全变色、中心无粉红为准。"
            note = f"{note} {reminder}".strip() if note else reminder
        elif any(word in instruction for word in _STIR_FRY_VEGETABLE_WORDS):
            minimum = 180 if "变软" in instruction else 120
            duration = max(duration, minimum)
            reminder = "火力和切配粗细会影响时间，以蔬菜实际断生或达到所需软度为准。"
            note = f"{note} {reminder}".strip() if note else reminder
        return instruction, duration, note

    @staticmethod
    def _timer_duration(step: dict[str, Any], instruction: str) -> int | float | None:
        duration = step.get("duration_seconds")
        if not isinstance(duration, (int, float)) or duration <= 0:
            return None
        # Timers are useful for heat-dependent cooking, not for washing,
        # chopping, mixing, seasoning, thawing or plating.
        return duration if any(action in instruction for action in _HEAT_ACTIONS) else None
