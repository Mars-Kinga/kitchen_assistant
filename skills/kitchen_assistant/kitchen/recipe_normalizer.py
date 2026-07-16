from __future__ import annotations

import re
from copy import deepcopy
from fractions import Fraction
from typing import Any

from .dish_profiles import matching_profiles
from .feedback_mapping import enrich_step


_HEAT_ACTIONS = (
    "预热", "加热", "烧开", "沸腾", "煮", "炖", "焖", "煎", "烤", "蒸",
    "炸", "熬", "焯", "炒", "收汁", "微波", "电饭煲",
)
_TIMED_PREP_ACTIONS = ("腌制", "浸泡", "泡发", "静置", "醒发")
_TIMED_ACTIONS = _HEAT_ACTIONS + _TIMED_PREP_ACTIONS
_STIR_FRY_MEAT_WORDS = ("猪肉", "猪里脊", "肉丝", "鸡肉", "牛肉丝")
_STIR_FRY_VEGETABLE_WORDS = (
    "蔬菜", "木耳", "胡萝卜", "青椒", "笋", "土豆", "茄子", "西兰花", "青菜", "洋葱",
)


class RecipeNormalizationError(ValueError):
    pass


class RecipeNormalizer:
    """Convert provider-specific dictionaries into the runtime Recipe shape."""

    def normalize(self, raw: dict[str, Any], *, servings: int | None = None) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise RecipeNormalizationError("菜谱不是对象")
        name = self._clean(raw.get("name"))
        if not name:
            raise RecipeNormalizationError("菜名不能为空")
        source_servings = self._servings(raw.get("servings") or raw.get("default_servings"))
        # Older local recipes were written as single-serving examples without
        # declaring that fact.  Once the user explicitly chooses a serving
        # count, use one serving as that legacy baseline so the ingredient
        # list and step quantities stay consistent.
        if source_servings is None and servings:
            source_servings = 1
        target_servings = servings or source_servings
        original_ingredients = self._ingredients(raw.get("ingredients"))
        ingredients = self._scale_ingredients(
            original_ingredients,
            source_servings,
            target_servings,
        )
        factor = Fraction(target_servings, source_servings) if source_servings and target_servings else Fraction(1, 1)
        quantity_replacements = self._quantity_replacements(original_ingredients, ingredients, factor)
        if not ingredients:
            raise RecipeNormalizationError("菜谱必须包含食材")
        raw_steps = raw.get("steps")
        if not isinstance(raw_steps, list) or not raw_steps:
            raise RecipeNormalizationError("菜谱必须包含至少一个步骤")
        prepared_steps: list[dict[str, Any]] = []
        for index, step in enumerate(raw_steps, start=1):
            instruction = self._clean(step.get("instruction") if isinstance(step, dict) else "")
            instruction = self._apply_quantity_replacements(instruction, quantity_replacements)
            if not instruction:
                raise RecipeNormalizationError(f"第 {index} 步缺少说明")
            if any(marker in instruction for marker in ("适量", "少量", "少许", "按口味")):
                raise RecipeNormalizationError(f"第 {index} 步包含模糊用量")
            if self._is_vague_parallel_prep_placeholder(instruction):
                # This is an AI-generated narration placeholder rather than
                # an executable action.  A later quantified mixing step will
                # be offered during the timer instead.
                continue
            instruction = self._remove_thaw_clauses(instruction)
            instruction = self._avoid_raw_meat_washing(instruction)
            if not instruction:
                continue
            normalized_step = {**step, "instruction": instruction}
            for expanded_step in self._split_preparation_step(normalized_step):
                expanded_instruction = str(expanded_step["instruction"])
                duration = self._timer_duration(expanded_step, expanded_instruction)
                expanded_instruction, duration = self._enforce_marinade_timer(
                    expanded_instruction,
                    duration,
                )
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
        # Cached AI recipes may have been saved while an older normalizer
        # split one marinade into “调味腌制” + “腌制10分钟”.  Repair that
        # representation on read so an old cache never asks for the same
        # marination and timer twice.
        prepared_steps = self._collapse_duplicate_marinade_steps(prepared_steps)
        prepared_steps = self._apply_profile_workflow_rules(prepared_steps, ingredients, name)
        prepared_steps = self._apply_profile_timer_steps(prepared_steps, name)
        cleaned_steps = [
            enrich_step(step, index, len(prepared_steps))
            for index, step in enumerate(prepared_steps, start=1)
        ]
        return {
            "recipe_id": self._clean(raw.get("recipe_id")) or f"normalized_{name}",
            "name": name,
            "source_name": self._clean(raw.get("source_name")) or "本地示例菜谱",
            "source_url": raw.get("source_url") or None,
            "servings": target_servings,
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
    def _servings(value: Any) -> int | None:
        return int(value) if isinstance(value, int) and value > 0 else None

    @classmethod
    def _scale_ingredients(
        cls,
        ingredients: list[dict[str, Any]],
        source_servings: int | None,
        target_servings: int | None,
    ) -> list[dict[str, Any]]:
        """Scale local recipe quantities when the user chose a serving count."""
        if not source_servings or not target_servings or source_servings == target_servings:
            return ingredients
        factor = Fraction(target_servings, source_servings)
        scaled: list[dict[str, Any]] = []
        for item in ingredients:
            copy = dict(item)
            copy["amount"] = cls._scale_amount(str(copy.get("amount") or ""), factor)
            scaled.append(copy)
        return scaled

    @staticmethod
    def _scale_amount(amount: str, factor: Fraction) -> str:
        def replace(match: re.Match[str]) -> str:
            token = match.group(0)
            value = Fraction(token) if "/" in token else Fraction(token)
            scaled = value * factor
            if scaled.denominator == 1:
                return str(scaled.numerator)
            if scaled.denominator in {2, 3, 4, 8}:
                return f"{scaled.numerator}/{scaled.denominator}"
            return f"{float(scaled):.1f}".rstrip("0").rstrip(".")

        return re.sub(r"\d+/\d+|\d+(?:\.\d+)?", replace, amount)

    @staticmethod
    def _quantity_replacements(
        original: list[dict[str, Any]],
        scaled: list[dict[str, Any]],
        factor: Fraction,
    ) -> list[tuple[str, str]]:
        replacements: list[tuple[str, str]] = []
        for before, after in zip(original, scaled):
            source_amount = str(before.get("amount") or "")
            target_amount = str(after.get("amount") or "")
            unit = str(before.get("unit") or "")
            target_unit = str(after.get("unit") or "")
            if source_amount == target_amount:
                continue
            if unit:
                replacements.append((f"{source_amount}{unit}", f"{target_amount}{target_unit}"))
                replacements.append((f"{source_amount} {unit}", f"{target_amount} {target_unit}"))
            for match in re.finditer(r"(\d+/\d+|\d+(?:\.\d+)?)\s*([^+\d\s][^+]*)", source_amount):
                source_value, source_unit = match.group(1), match.group(2).strip()
                target_value = RecipeNormalizer._scale_amount(source_value, factor)
                replacements.append((f"{source_value} {source_unit}", f"{target_value} {source_unit}"))
                replacements.append((f"{source_value}{source_unit}", f"{target_value}{source_unit}"))
        return sorted(set(replacements), key=lambda item: len(item[0]), reverse=True)

    @staticmethod
    def _apply_quantity_replacements(instruction: str, replacements: list[tuple[str, str]]) -> str:
        for source, target in replacements:
            if source and target:
                instruction = instruction.replace(source, target)
        return instruction

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
    def _avoid_raw_meat_washing(instruction: str) -> str:
        """Do not instruct a beginner to rinse raw meat at the sink."""
        meat = r"(?:排骨|牛肉片?|牛肉丁|鸡肉|鸡翅|鸡胸肉|猪肉)"
        return re.sub(rf"(?P<meat>{meat})洗净后", r"\g<meat>用厨房纸擦干表面后", instruction)

    @staticmethod
    def _is_vague_parallel_prep_placeholder(instruction: str) -> bool:
        """Discard non-actions such as “腌制期间准备调料碗”."""
        waiting = any(marker in instruction for marker in ("腌制期间", "等待期间"))
        generic_bowl = any(marker in instruction for marker in ("准备调料碗", "准备调料", "准备酱汁"))
        concrete_seasonings = ("生抽", "老抽", "米醋", "醋", "白砂糖", "白糖", "盐", "料酒", "蚝油", "胡椒")
        return waiting and generic_bowl and not any(item in instruction for item in concrete_seasonings)

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

        for part_index, part in enumerate(parts):
            if "泡发" in part:
                flush()
                groups.append([part])
                current_kind = "prep"
                continue
            if "腌制" in part:
                current.append(part)
                # A provider commonly describes one marination twice, for
                # example: “抓匀，静置腌制，腌制10分钟”.  Do not turn that
                # into a second waiting step (and therefore a second timer).
                # Keep consecutive marinade clauses together, then split
                # before the next real preparation action instead.
                next_part = parts[part_index + 1] if part_index + 1 < len(parts) else ""
                if "腌制" not in next_part:
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
    def _collapse_duplicate_marinade_steps(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Keep one timer when old cached recipes repeat a bare marinade step."""
        collapsed: list[dict[str, Any]] = []
        bare_marinade = re.compile(
            r"^(?:请)?(?:继续)?(?:静置)?腌制(?:\d+(?:\.\d+)?|[一二两三四五六七八九十百]+)?(?:分钟|分|秒)?(?:即可)?$"
        )
        for step in steps:
            instruction = str(step.get("instruction", "")).replace(" ", "").strip("，,；;。 ")
            previous = collapsed[-1] if collapsed else None
            previous_instruction = str(previous.get("instruction", "")) if previous else ""
            if (
                previous is not None
                and "调味汁" in previous_instruction
                and "调味汁" in instruction
                and "腌制" in previous_instruction
                and "腌制" in instruction
            ):
                previous_base = re.sub(
                    r"[，,、]?(?:静置)?腌制(?:\d+(?:\.\d+)?|[一二两三四五六七八九十百]+)?(?:分钟|分|秒)?",
                    "",
                    previous_instruction,
                ).strip("，,、；; ")
                previous["instruction"] = f"{previous_base}，{instruction}" if previous_base else instruction
                previous_duration = previous.get("duration_seconds")
                current_duration = step.get("duration_seconds")
                if isinstance(current_duration, (int, float)):
                    previous["duration_seconds"] = max(previous_duration or 0, current_duration) or None
                continue
            if (
                previous is not None
                and "腌制" in previous_instruction
                and "腌制好的" not in previous_instruction
                and "腌制" in instruction
                and "腌制好的" not in instruction
                and "加入" in instruction
                and not any(marker in f"{previous_instruction}{instruction}" for marker in ("调味汁", "酱汁"))
            ):
                previous_base = re.sub(
                    r"[，,、]?(?:静置)?腌制(?:\d+(?:\.\d+)?|[一二两三四五六七八九十百]+)?(?:分钟|分|秒)?",
                    "",
                    previous_instruction,
                ).strip("，,、；; ")
                current_instruction = re.sub(r"腌制[，,、]\s*腌制", "腌制", instruction)
                previous["instruction"] = f"{previous_base}，{current_instruction}" if previous_base else current_instruction
                previous_duration = previous.get("duration_seconds")
                current_duration = step.get("duration_seconds")
                if isinstance(current_duration, (int, float)):
                    previous["duration_seconds"] = max(previous_duration or 0, current_duration) or None
                continue
            if (
                previous is not None
                and re.search(r"腌制(?!好)", previous_instruction)
                and bare_marinade.fullmatch(instruction)
            ):
                previous_duration = previous.get("duration_seconds")
                current_duration = step.get("duration_seconds")
                if isinstance(current_duration, (int, float)):
                    previous["duration_seconds"] = max(previous_duration or 0, current_duration) or None
                if not previous.get("safety_note") and step.get("safety_note"):
                    previous["safety_note"] = step["safety_note"]
                continue
            collapsed.append(step)
        return collapsed

    @staticmethod
    def _enforce_realistic_timing(
        instruction: str,
        duration: int | float | None,
        safety_note: Any,
    ) -> tuple[str, int | float | None, str | None]:
        """Clamp obviously short AI timers and add observable doneness checks."""
        note = str(safety_note or "").strip() or None
        if "炒" not in instruction:
            return instruction, duration, note
        if any(word in instruction for word in _STIR_FRY_MEAT_WORDS):
            # AI responses occasionally omit duration_seconds entirely. A
            # missing timer on a raw-meat stir-fry is unsafe, so provide a
            # conservative lower-bound reference instead of leaving it null.
            duration = max(duration or 120, 120)
            if not any(marker in instruction for marker in ("无粉红", "完全变色", "熟透")):
                instruction = f"{instruction}，炒到肉类完全变色、中心无粉红"
            reminder = "计时只是下限参考，应以肉类完全变色、中心无粉红为准。"
            note = (note or "").replace("肉丝完全变色", "肉类完全变色") or None
            note = f"{note} {reminder}".strip() if note else reminder
        elif any(word in instruction for word in _STIR_FRY_VEGETABLE_WORDS):
            minimum = 180 if "变软" in instruction else 120
            duration = max(duration or minimum, minimum)
            reminder = "火力和切配粗细会影响时间，以蔬菜实际断生或达到所需软度为准。"
            note = f"{note} {reminder}".strip() if note else reminder
        return instruction, duration, note

    @staticmethod
    def _enforce_marinade_timer(
        instruction: str,
        duration: int | float | None,
    ) -> tuple[str, int | float | None]:
        """Make an otherwise vague marination step actionable and timed."""
        if not re.search(r"腌制(?!好)", instruction):
            return instruction, duration
        seconds = int(duration or 600)
        if not re.search(r"(?:\d+(?:\.\d+)?|[一二两三四五六七八九十百]+)\s*(?:小时|分钟|分|秒)", instruction):
            minutes = seconds // 60
            suffix = f"腌制{minutes}分钟" if seconds % 60 == 0 else f"腌制{seconds}秒"
            instruction = f"{instruction}，{suffix}"
        return instruction, seconds

    @staticmethod
    def _apply_profile_workflow_rules(
        steps: list[dict[str, Any]],
        ingredients: list[dict[str, Any]],
        recipe_name: str,
    ) -> list[dict[str, Any]]:
        """Apply declarative per-dish ordering rules from recipes.json."""
        ingredient_names = " ".join(str(item.get("name", "")) for item in ingredients)
        for profile in matching_profiles(recipe_name, ingredient_names):
            rule = profile.get("ensure_predecessor")
            if not isinstance(rule, dict):
                continue
            waiting_marker = str(rule.get("waiting_marker", ""))
            predecessor_markers = tuple(str(item) for item in rule.get("predecessor_markers", []) if str(item))
            if not waiting_marker or not predecessor_markers:
                continue
            waiting_index = next((index for index, step in enumerate(steps) if waiting_marker in str(step.get("instruction", "")) and f"{waiting_marker}好" not in str(step.get("instruction", ""))), None)
            if waiting_index is None:
                continue
            predecessor_index = next((index for index, step in enumerate(steps) if any(marker in str(step.get("instruction", "")) for marker in predecessor_markers)), None)
            if predecessor_index is None:
                inserted = rule.get("insert_step")
                if isinstance(inserted, dict):
                    steps.insert(waiting_index, dict(inserted))
            elif predecessor_index > waiting_index:
                steps.insert(waiting_index, steps.pop(predecessor_index))
        return steps

    @staticmethod
    def _apply_profile_timer_steps(steps: list[dict[str, Any]], recipe_name: str) -> list[dict[str, Any]]:
        """Attach data-driven timer transitions to matching recipe steps."""
        for profile in matching_profiles(recipe_name):
            for rule in profile.get("timer_steps", []):
                if not isinstance(rule, dict):
                    continue
                markers = tuple(str(item) for item in rule.get("matches", []) if str(item))
                if not markers:
                    continue
                for step in steps:
                    instruction = str(step.get("instruction", ""))
                    if all(marker in instruction for marker in markers):
                        for key in ("timer_label", "timer_end_action", "confirmation_markers", "waiting_speech", "waiting_display", "timer_end_speech", "timer_end_display"):
                            if key in rule:
                                step[key] = rule[key]
        return steps

    @staticmethod
    def _timer_duration(step: dict[str, Any], instruction: str) -> int | float | None:
        duration = step.get("duration_seconds")
        has_timed_action = any(action in instruction for action in _TIMED_ACTIONS)
        if not has_timed_action:
            # Timers are not added to washing, chopping, mixing, seasoning,
            # thawing or plating steps.
            return None
        if isinstance(duration, (int, float)) and duration > 0:
            return duration
        # AI providers often put “腌制10分钟” in the prose but omit the
        # structured duration field. Recover that explicit duration so the
        # user can start a real timer instead of relying on memory.
        match = re.search(r"(\d+(?:\.\d+)?)\s*(小时|分钟|分|秒)", instruction)
        if match:
            amount = float(match.group(1))
            unit = match.group(2)
        else:
            match = re.search(r"([一二两三四五六七八九十百]+)\s*(小时|分钟|分|秒)", instruction)
            if not match:
                return None
            amount = float(RecipeNormalizer._chinese_number(match.group(1)))
            unit = match.group(2)
        multiplier = 3600 if unit == "小时" else 60 if unit in {"分钟", "分"} else 1
        return int(amount * multiplier)

    @staticmethod
    def _chinese_number(value: str) -> int:
        digits = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
        if value == "十":
            return 10
        if "十" in value:
            left, _, right = value.partition("十")
            return (digits.get(left, 1) * 10 if left else 10) + (digits.get(right, 0) if right else 0)
        if "百" in value:
            left, _, right = value.partition("百")
            return digits.get(left, 1) * 100 + RecipeNormalizer._chinese_number(right) if right else digits.get(left, 1) * 100
        return digits.get(value, 0)
