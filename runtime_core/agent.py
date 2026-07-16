from __future__ import annotations

from typing import Any


_COOKING_QUESTION_MARKERS = (
    "怎么做", "如何做", "做法", "教程", "怎么烧", "怎么煮", "怎么炒", "怎么炖", "怎么焖", "怎么煲",
    "教我烧", "教我煮", "教我炖", "教我弄", "想弄",
)
_COOKING_FOOD_MARKERS = (
    "菜", "汤", "面", "饭", "粥", "排骨", "肉", "鸡", "鱼", "虾", "蛋", "豆腐", "牛排",
    "肥牛", "番茄", "土豆", "茄子", "青菜", "西兰花", "香菇", "饺子", "火锅", "意面",
)


class SkillAgent:
    """Select a Skill from installed skill.json manifests.

    This minimal Agent uses trigger matching first, which keeps the runtime
    runnable without any model API key. A real LLM router can be plugged in
    later behind the same return shape.
    """

    def select_skill(self, user_text: str, registry: list[dict[str, Any]]) -> dict[str, Any]:
        best_skill: dict[str, Any] | None = None
        best_hits: list[str] = []
        best_priority = 0

        for skill in registry:
            negative_hits = [
                word for word in skill.get("negative_triggers", [])
                if isinstance(word, str) and word and word in user_text
            ]
            if negative_hits:
                continue

            hits = [
                word for word in skill.get("triggers", [])
                if isinstance(word, str) and word and word in user_text
            ]
            if skill.get("name") == "kitchen_assistant" and self._has_cooking_intent(user_text):
                hits.append("烹饪问法")
            if not hits:
                continue
            priority = int(skill.get("priority", 0))
            if len(hits) > len(best_hits) or (len(hits) == len(best_hits) and priority > best_priority):
                best_skill = skill
                best_hits = hits
                best_priority = priority

        if best_skill is None:
            return {
                "route": "normal_chat",
                "selected_skill": None,
                "reason": "没有命中已安装 Skill，按普通聊天处理。",
            }

        return {
            "route": "skill_call",
            "selected_skill": best_skill["name"],
            "reason": f"命中 {best_skill['display_name']} 的触发词。",
            "matched_evidence": best_hits,
        }

    @staticmethod
    def _has_cooking_intent(user_text: str) -> bool:
        """Recognize food-specific how-to questions without capturing generic how-to chat."""
        compact = str(user_text or "").replace(" ", "")
        pantry_markers = ("我有", "家里有", "冰箱里", "现有", "手边有", "手头有", "手上有", "只有", "只剩")
        return (
            any(marker in compact for marker in _COOKING_QUESTION_MARKERS)
            and any(marker in compact for marker in _COOKING_FOOD_MARKERS)
        ) or (
            any(marker in compact for marker in pantry_markers)
            and any(marker in compact for marker in _COOKING_FOOD_MARKERS)
        )
