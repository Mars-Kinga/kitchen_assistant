from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .ingredient_vocabulary import canonicalize_ingredient, inventory_ingredient_present


COMMON_SEASONING_NAMES = {
    "食用油": ("食用油", "油", "植物油", "花生油", "菜籽油", "玉米油", "葵花籽油", "大豆油"),
    "盐": ("盐", "食盐", "食用盐", "海盐"),
    "白糖": ("白糖", "白砂糖", "绵白糖", "细砂糖"),
    "生抽": ("生抽", "酱油", "普通酱油", "味极鲜"),
    "老抽": ("老抽",),
    "醋": ("醋", "米醋", "陈醋", "香醋", "白醋"),
    "料酒": ("料酒",),
    "蚝油": ("蚝油",),
    "胡椒粉": ("胡椒", "胡椒粉", "白胡椒", "白胡椒粉", "黑胡椒", "黑胡椒粉"),
    "淀粉": ("淀粉", "生粉", "玉米淀粉", "土豆淀粉", "红薯淀粉"),
    "香油": ("香油", "芝麻油", "麻油"),
    "葱": ("葱", "小葱", "香葱", "大葱", "葱花", "葱段", "葱白"),
    "姜": ("姜", "生姜", "姜片", "姜丝", "姜末"),
    "蒜": ("蒜", "大蒜", "蒜头", "蒜瓣", "蒜片", "蒜末", "蒜泥"),
}
COMMON_SEASONINGS = tuple(COMMON_SEASONING_NAMES)


def pantry_meal_rule(request: dict[str, Any]) -> str | None:
    if not request.get("available_ingredients") or request.get("requested_dish"):
        return None
    return (
        "每个candidate代表一份备选用餐方案，不代表只能做一道菜；候选是备选方案数，不是每份方案的菜数。优先提供2份完整方案，只有1份可行时也返回，不为凑数量输出不完整菜谱。"
        "食材适合分开做时，优先组合两道或三道菜，也可搭配汤或饮品；食材简单时允许单道菜，不为凑菜数额外添加主食材。"
        "优先让整套饭菜合起来使用全部确认食材，不要求每一道菜都用全部食材，禁止为了全覆盖硬塞进同一道菜。全覆盖不可行时允许没用到部分食材，仍返回完整可做的方案，按实际使用种数从多到少排序。程序根据配料和实际步骤计算没用到的食材，不能虚报全覆盖。"
        "组合title按制作顺序用‘＋’列出各道菜名；一个recipe保存整套方案，ingredients列总用量，"
        "每道菜的步骤以【第1道】、【第2道】、【第3道】标明归属，与title顺序对应，不把菜名写入步骤前缀冒充实际操作。"
        "每道菜分别写清切配、调味、下锅、火力、计时和装盘；共用调料在各道步骤分配准确用量且合计与ingredients一致。"
        "按现有逐步指导顺序执行，不安排多锅同时加热；estimated_minutes估算完成整套饭菜的总时间，不只计算主菜。"
    )


def missing_pantry_ingredients(labels: Iterable[str], inventory: Iterable[str]) -> list[str]:
    available = list(inventory)
    return [name for name in labels
            if not is_common_seasoning(name) and name not in {"水", "清水", "热水", "开水"}
            and not any(inventory_ingredient_present(item, [name]) for item in available)]


def common_seasoning_kind(name: str) -> str | None:
    value = canonicalize_ingredient(name)
    return next((kind for kind, names in COMMON_SEASONING_NAMES.items() if value in names), None)


def is_common_seasoning(name: str) -> bool:
    return common_seasoning_kind(name) is not None


def uses_unavailable_ingredients(labels: Iterable[str], unavailable: Iterable[str]) -> bool:
    names = list(labels)
    for missing in unavailable:
        if missing in {"调料", "调味料"} and any(is_common_seasoning(name) for name in names):
            return True
        if inventory_ingredient_present(missing, names):
            return True
        kind = common_seasoning_kind(missing)
        if kind and any(common_seasoning_kind(name) == kind for name in names):
            return True
        if missing == "油" and any(common_seasoning_kind(name) in {"食用油", "香油"} for name in names):
            return True
    return False


def pantry_seasoning_rule(request: dict[str, Any]) -> str | None:
    if not request.get("available_ingredients") or request.get("requested_dish"):
        return None
    return (
        f"按食材推荐时，available_ingredients是用户拍照确认要吃的食材，不是完整厨房库存；"
        f"默认家中有常见基础调料：{'、'.join(COMMON_SEASONINGS)}，无需用户拍照提供，按菜品需要选用，不要全部堆入。"
        "默认调料不追加到available_ingredients，不计入全食材覆盖或排序，也不列为缺少；missing_ingredients=[]。"
        "实际使用的调料仍须列入recipe.ingredients及步骤，写清准确用量；遵守少盐、忌口和unavailable_ingredients，用户明确没有的不能使用。"
        "不要默认有蜂蜜、黄油、奶油、啤酒、红酒、咖喱块等特殊配料；未确认的特殊配料只能作为可选，不要求用户额外准备主食材。"
    )
