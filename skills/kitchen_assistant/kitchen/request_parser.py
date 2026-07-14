from __future__ import annotations

import re
from dataclasses import dataclass, field

from .intent_parser import extract_flavor, extract_servings
from .models import RecipeSearchRequest


KNOWN_DISHES = (
    "红烧排骨", "糖醋排骨", "红烧肉", "水煮肉片", "鱼香肉丝", "麻婆豆腐", "宫保鸡丁", "回锅肉",
    "清蒸鱼", "红烧鱼", "番茄肥牛", "肥牛饭", "煎牛排", "番茄鸡蛋汤", "番茄鸡蛋面",
    "青菜鸡蛋面", "简单汤面", "番茄炒蛋", "蛋炒饭", "炒饭", "炒面", "牛肉面", "意面",
    "蒜蓉西兰花", "炒青菜", "鸡蛋羹", "土豆丝", "可乐鸡翅", "咖喱饭", "牛排",
)
KNOWN_INGREDIENTS = (
    "鸡蛋", "番茄", "面条", "青菜", "土豆", "米饭", "鸡翅", "排骨", "五花肉", "猪肉", "鸡肉", "鱼", "虾",
    "可乐", "咖喱", "牛肉", "肥牛", "牛排", "豆腐", "茄子", "西兰花", "香菇", "洋葱",
    "葱", "姜", "蒜", "香菜", "八角", "冰糖", "白糖", "辣椒", "生抽", "老抽", "料酒", "蚝油",
    "橄榄油", "食用油", "黄油", "黑胡椒", "海盐", "盐", "迷迭香",
)
_DISH_ALIASES = {
    "西红柿炒蛋": "番茄炒蛋",
    "番茄炒鸡蛋": "番茄炒蛋",
    "西红柿鸡蛋汤": "番茄鸡蛋汤",
    "西红柿鸡蛋面": "番茄鸡蛋面",
    "可乐翅": "可乐鸡翅",
}
_DISH_REQUEST = re.compile(
    r"(?:我想做|我要做|想做|要做(?!什么)|帮我做|教我做|做一道|做一份|做个|来一份|来个)\s*([^，,。！？；;]+)"
)
_DISH_QUESTION = re.compile(
    r"(?:^|[，,。！？；;])\s*(?:请问|想问|我想知道|想知道|教我|帮我|能教我|可以教我)?\s*([^，,。！？；;]{2,30}?)(?:怎么做|如何做|做法|教程|怎么烧|怎么煮|怎么炒|怎么炖|怎么焖|怎么煲)"
)
_GENERIC_DISH_TEXT = {"饭", "饭了", "做饭", "做饭了", "菜", "菜了", "什么", "什么菜", "什么饭"}


@dataclass
class RequestUpdates:
    requested_dish: str | None = None
    ingredients: list[str] = field(default_factory=list)
    servings: int | None = None
    taste: str | None = None
    restrictions: list[str] = field(default_factory=list)
    max_minutes: int | None = None
    equipment: list[str] = field(default_factory=list)
    difficulty: str | None = None
    steak_doneness: str | None = None
    steak_thickness_cm: float | None = None
    asks_for_recommendation: bool = False


def parse_updates(text: str) -> RequestUpdates:
    updates = RequestUpdates()
    for alias, canonical in _DISH_ALIASES.items():
        if alias in text:
            updates.requested_dish = canonical
            break
    for dish in KNOWN_DISHES:
        if updates.requested_dish is None and dish in text:
            updates.requested_dish = dish
            break
    if updates.requested_dish is None:
        updates.requested_dish = _extract_requested_dish(text) or _extract_dish_question(text)
    # A dish name is a request, not proof that the user owns its ingredients.
    updates.ingredients = extract_ingredients(text)
    updates.servings = extract_servings(text)
    updates.taste = extract_flavor(text)
    if "辣" in text and "不吃辣" not in text and "不要辣" not in text:
        updates.taste = "辣"
    if "不吃辣" in text or "不要辣" in text:
        updates.restrictions.append("辣")
    for ingredient in KNOWN_INGREDIENTS:
        if any(phrase in text for phrase in (
            f"不吃{ingredient}", f"不要{ingredient}", f"不放{ingredient}",
            f"对{ingredient}过敏", f"{ingredient}过敏",
        )) and ingredient not in updates.restrictions:
            updates.restrictions.append(ingredient)
    match = re.search(r"(\d+|[一二两三四五六七八九十]+)\s*分钟(?:以内|内)?", text)
    if match:
        value = _parse_number(match.group(1))
        if value:
            updates.max_minutes = value
    if "快一点" in text or "更快" in text:
        updates.max_minutes = updates.max_minutes or 15
    if "简单一点" in text or "更简单" in text or "新手" in text:
        updates.difficulty = "简单"
    if "小锅" in text:
        updates.equipment.append("小锅")
    if "炒锅" in text:
        updates.equipment.append("炒锅")
    if "电饭煲" in text:
        updates.equipment.append("电饭煲")
    for doneness in ("三分熟", "五分熟", "七分熟", "全熟"):
        if doneness in text:
            updates.steak_doneness = doneness
            break
    thickness = re.search(r"(\d(?:\.\d+)?)\s*(?:厘米|cm)\s*(?:厚|左右|的)?", text, flags=re.IGNORECASE)
    if thickness:
        updates.steak_thickness_cm = float(thickness.group(1))
    elif any(word in text for word in ("普通厚度", "一般厚度", "不清楚多厚", "不知道多厚")):
        updates.steak_thickness_cm = 2.0
    updates.asks_for_recommendation = any(word in text for word in (
        "不知道", "有什么做什么", "怎么搭配", "现有的东西", "冰箱里", "推荐",
    )) and updates.requested_dish is None
    return updates


def _extract_requested_dish(text: str) -> str | None:
    """Extract a free-form dish name without mistaking “要做什么” for one."""
    matches = list(_DISH_REQUEST.finditer(text.replace("\n", " ")))
    if not matches:
        return None
    # A sentence such as “我想好要做什么了，要做番茄肥牛” has two
    # “要做” fragments. The final meaningful one is the user's actual dish.
    candidate = matches[-1].group(1).strip()
    candidate = re.sub(r"^(?:一道|一份|一个|个|份)\s*", "", candidate)
    candidate = re.sub(r"(?:的做法|怎么做|做法)$", "", candidate).strip()
    candidate = re.sub(r"^(?:两个人吃的|一个人吃的|一人份的|少盐的|清淡的|正常口味的)", "", candidate).strip()
    compact = candidate.replace(" ", "")
    if compact in _GENERIC_DISH_TEXT or not 2 <= len(compact) <= 30:
        return None
    return compact


def _extract_dish_question(text: str) -> str | None:
    """Accept natural questions such as “红烧排骨怎么做” as dish requests."""
    matches = list(_DISH_QUESTION.finditer(text.replace("\n", " ")))
    if not matches:
        return None
    candidate = matches[-1].group(1).strip()
    candidate = re.sub(r"^(?:这个|那道|这道|菜|饭|请问|想问|我想知道|想知道|教我|帮我)", "", candidate).strip()
    candidate = re.sub(r"(?:的)?(?:菜|饭)$", "", candidate).strip()
    compact = candidate.replace(" ", "")
    if compact in _GENERIC_DISH_TEXT or not 2 <= len(compact) <= 30:
        return None
    return compact


def _without_requested_dish_span(text: str) -> str:
    """Exclude only the explicit request phrase when scanning the pantry."""
    matches = list(_DISH_REQUEST.finditer(text.replace("\n", " ")))
    if not matches:
        return text
    match = matches[-1]
    return f"{text[:match.start(1)]}{text[match.end(1):]}"


def _has_inventory_signal(text: str) -> bool:
    return any(marker in text for marker in ("我有", "家里有", "冰箱有", "冰箱里", "现有", "还有", "手边有"))


def extract_ingredients(text: str, *, allow_bare: bool = False) -> list[str]:
    """Extract an explicit pantry list, or a bare list after the pantry prompt."""
    inventory_text = _without_requested_dish_span(text)
    if not allow_bare and not _has_inventory_signal(inventory_text):
        return []
    return [item for item in KNOWN_INGREDIENTS if item in inventory_text]


def apply_updates(request: RecipeSearchRequest, updates: RequestUpdates) -> RecipeSearchRequest:
    if updates.requested_dish:
        request.requested_dish = updates.requested_dish
    _extend_unique(request.available_ingredients, updates.ingredients)
    if updates.servings:
        request.servings = updates.servings
    if updates.taste:
        request.taste_preferences = [updates.taste]
    _extend_unique(request.dietary_restrictions, updates.restrictions)
    if updates.max_minutes:
        request.max_cooking_minutes = updates.max_minutes
    _extend_unique(request.available_equipment, updates.equipment)
    if updates.difficulty:
        request.difficulty_preference = updates.difficulty
    if updates.steak_doneness:
        request.steak_doneness = updates.steak_doneness
    if updates.steak_thickness_cm is not None:
        request.steak_thickness_cm = updates.steak_thickness_cm
    return request


def select_candidate(text: str, titles: list[str]) -> int | None:
    compact = text.replace(" ", "").strip()
    if compact in {"1", "一"}:
        return 0 if titles else None
    if compact in {"2", "二"}:
        return 1 if len(titles) > 1 else None
    if compact in {"3", "三"}:
        return 2 if len(titles) > 2 else None
    ordinals = (("第一个", 0), ("选一", 0), ("第1", 0), ("第一个菜", 0), ("第二个", 1), ("选二", 1), ("第2", 1), ("第三个", 2), ("选三", 2), ("第3", 2))
    for phrase, index in ordinals:
        if phrase in text:
            return index if index < len(titles) else None
    for index, title in enumerate(titles):
        if title in text:
            return index
    return None


def _extend_unique(target: list[str], additions: list[str]) -> None:
    for value in additions:
        if value not in target:
            target.append(value)


def _parse_number(value: str) -> int | None:
    if value.isdigit():
        return int(value)
    mapping = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10, "二十": 20, "三十": 30}
    return mapping.get(value)
