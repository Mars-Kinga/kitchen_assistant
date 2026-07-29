from __future__ import annotations

import re
from collections.abc import Iterable, Mapping


# 只放“可以无损替换”的写法差异和常见语音识别误差。类别关系放在
# INGREDIENT_GROUPS，避免把“牛排”直接改写成“牛肉”后丢失食材细节。
INGREDIENT_NAME_ALIASES: dict[str, str] = {
    "西红柿": "番茄",
    "马铃薯": "土豆",
    "洋芋": "土豆",
    "胡萝": "胡萝卜",
    "胡罗卜": "胡萝卜",
    "胡萝贝": "胡萝卜",
    "包菜": "卷心菜",
    "圆白菜": "卷心菜",
    "莲花白": "卷心菜",
    "花菜": "菜花",
    "西蓝花": "西兰花",
    "小葱": "葱",
    "香葱": "葱",
    "生姜": "姜",
    "大蒜": "蒜",
    "蒜头": "蒜",
    "香芫荽": "香菜",
    "芫荽": "香菜",
    "玉蜀黍": "玉米",
    "苞米": "玉米",
    "青豆": "豌豆",
    "四季豆": "豆角",
    "刀豆": "豆角",
    "云吞": "馄饨",
    "白胡椒面": "白胡椒粉",
    "黑胡椒面": "黑胡椒粉",
    "芝麻油": "香油",
    "麻油": "香油",
}


# 日常做饭中的“同类可识别关系”。它用于库存命中、候选详情一致性检查和
# 忌口判断；不表示这些食材在任何菜谱中都可以互相替换。
INGREDIENT_GROUPS: dict[str, tuple[str, ...]] = {
    "菌菇": (
        "菌菇", "蘑菇", "香菇", "鲜香菇", "干香菇", "口蘑", "平菇", "白玉菇",
        "金针菇", "杏鲍菇", "蟹味菇", "海鲜菇", "茶树菇", "草菇", "木耳", "银耳",
    ),
    "牛肉": (
        "牛肉", "肥牛", "牛腩", "牛里脊", "牛排", "牛肉片", "牛肉丝", "牛肉末",
        "牛肉馅", "牛筋", "牛腱",
    ),
    "猪肉": (
        "猪肉", "五花肉", "猪里脊", "里脊肉", "梅花肉", "排骨", "猪排", "猪肉末",
        "猪肉馅", "猪蹄", "猪肝", "培根", "火腿", "腊肉",
    ),
    "鸡肉": (
        "鸡肉", "鸡胸", "鸡胸肉", "鸡腿", "鸡腿肉", "鸡翅", "鸡翅中", "鸡翅根",
        "鸡丁", "鸡肉片", "鸡肉丝", "鸡肉末", "整鸡", "鸡爪",
    ),
    "鸭肉": ("鸭肉", "鸭腿", "鸭胸", "鸭翅", "鸭块", "整鸭"),
    "羊肉": ("羊肉", "羊排", "羊腿", "羊肉片", "羊肉卷", "羊肉串", "羊肉馅"),
    "鱼": (
        "鱼", "鲜鱼", "鱼片", "鱼块", "鱼柳", "鲈鱼", "鳕鱼", "三文鱼", "草鱼", "鲫鱼",
        "鲤鱼", "带鱼", "黄花鱼", "龙利鱼", "巴沙鱼", "鲳鱼", "黑鱼", "金枪鱼",
    ),
    "虾": ("虾", "鲜虾", "虾仁", "鲜虾仁", "大虾", "基围虾", "明虾", "河虾", "小龙虾", "虾滑"),
    "蟹": ("蟹", "螃蟹", "大闸蟹", "梭子蟹", "蟹肉", "蟹柳"),
    "贝类": ("贝", "贝类", "蛤蜊", "花甲", "扇贝", "生蚝", "牡蛎", "青口", "蛏子"),
    "头足类": ("鱿鱼", "章鱼", "墨鱼", "八爪鱼"),
    "鸡蛋": ("鸡蛋", "蛋", "鸭蛋", "鹌鹑蛋", "皮蛋", "蛋液", "蛋黄", "蛋清"),
    "豆腐": (
        "豆腐", "嫩豆腐", "老豆腐", "北豆腐", "南豆腐", "内酯豆腐", "冻豆腐",
        "豆干", "香干", "千张", "豆皮", "腐竹", "油豆腐", "豆泡",
    ),
    "大豆": ("大豆", "黄豆", "毛豆", "豆浆", "豆腐", "豆干", "豆皮", "腐竹", "豆豉"),
    "面条": ("面条", "挂面", "手擀面", "乌冬面", "拉面", "米线", "河粉", "意面", "方便面"),
    "米饭": ("米饭", "熟米饭", "剩米饭", "隔夜饭", "糙米饭", "杂粮饭"),
    "米": ("大米", "糯米", "小米", "糙米", "黑米", "粳米", "籼米"),
    "青菜": (
        "青菜", "小白菜", "上海青", "油菜", "菜心", "菠菜", "生菜", "油麦菜",
        "娃娃菜", "空心菜", "苋菜", "芥蓝", "茼蒿",
    ),
    "白菜": ("白菜", "大白菜", "娃娃菜"),
    "卷心菜": ("卷心菜", "包菜", "圆白菜", "莲花白", "紫甘蓝"),
    "菜花": ("菜花", "花菜", "西兰花", "西蓝花", "有机花菜"),
    "豆角": ("豆角", "四季豆", "刀豆", "豇豆", "扁豆", "荷兰豆"),
    "豆芽": ("豆芽", "绿豆芽", "黄豆芽"),
    "辣椒": ("辣椒", "青椒", "红椒", "彩椒", "甜椒", "尖椒", "线椒", "小米辣", "朝天椒"),
    "玉米": ("玉米", "玉米粒", "甜玉米", "玉米棒"),
    "葱": ("葱", "小葱", "香葱", "大葱", "葱花", "葱段", "葱白"),
    "姜": ("姜", "生姜", "姜片", "姜丝", "姜末"),
    "蒜": ("蒜", "大蒜", "蒜头", "蒜瓣", "蒜片", "蒜末", "蒜泥", "青蒜"),
    "食用油": (
        "食用油", "植物油", "花生油", "菜籽油", "玉米油", "葵花籽油", "大豆油",
        "橄榄油", "米糠油", "茶籽油", "猪油", "黄油", "椰子油",
    ),
    "酱油": ("酱油", "普通酱油", "生抽", "老抽", "味极鲜", "蒸鱼豉油", "豉油"),
    "醋": ("醋", "米醋", "陈醋", "香醋", "白醋", "果醋"),
    "糖": ("糖", "白糖", "白砂糖", "绵白糖", "冰糖", "红糖", "蜂蜜"),
    "淀粉": ("淀粉", "玉米淀粉", "土豆淀粉", "红薯淀粉", "生粉"),
    "奶制品": ("牛奶", "奶", "淡奶油", "奶油", "芝士", "奶酪", "黄油", "酸奶", "炼乳"),
    "花生": ("花生", "花生米", "花生碎", "花生酱", "花生油"),
    "芝麻": ("芝麻", "白芝麻", "黑芝麻", "芝麻酱", "香油", "芝麻油", "麻油"),
    "坚果": ("坚果", "核桃", "腰果", "杏仁", "开心果", "榛子", "松子", "板栗", "花生"),
}


# 忌口类别比库存类别更宽。例如“海鲜”会命中鱼虾蟹贝；“辣”只匹配
# 明确有辣度的配料，不把青椒和彩椒一概判断为辣。
DIETARY_RESTRICTION_GROUPS: dict[str, tuple[str, ...]] = {
    **INGREDIENT_GROUPS,
    "海鲜": tuple(dict.fromkeys(
        INGREDIENT_GROUPS["鱼"]
        + INGREDIENT_GROUPS["虾"]
        + INGREDIENT_GROUPS["蟹"]
        + INGREDIENT_GROUPS["贝类"]
        + INGREDIENT_GROUPS["头足类"]
    )),
    "甲壳类": tuple(dict.fromkeys(INGREDIENT_GROUPS["虾"] + INGREDIENT_GROUPS["蟹"])),
    "蛋类": INGREDIENT_GROUPS["鸡蛋"],
    "乳制品": INGREDIENT_GROUPS["奶制品"],
    "奶": INGREDIENT_GROUPS["奶制品"],
    "大豆制品": tuple(dict.fromkeys(INGREDIENT_GROUPS["大豆"] + INGREDIENT_GROUPS["豆腐"])),
    "辣": (
        "辣椒", "干辣椒", "辣椒段", "辣椒粉", "辣椒面", "小米辣", "朝天椒", "尖椒",
        "线椒", "泡椒", "剁椒", "辣酱", "辣椒酱", "豆瓣酱", "火锅底料", "芥末",
    ),
    "酒精": ("酒", "白酒", "黄酒", "啤酒", "红酒", "葡萄酒", "米酒", "料酒", "酒酿"),
    "麸质": ("小麦", "面粉", "普通面粉", "高筋面粉", "中筋面粉", "低筋面粉", "面包", "面条", "馒头", "饺子皮"),
}


SEASONING_MARKERS = (
    "盐", "糖", "生抽", "老抽", "酱油", "豉油", "醋", "料酒", "黄酒", "白酒",
    "蚝油", "鱼露", "豆瓣", "胡椒", "花椒", "辣椒粉", "辣椒面", "辣酱", "鸡精",
    "味精", "五香粉", "十三香", "孜然", "咖喱", "味噌", "沙茶", "食用油", "植物油",
    "花生油", "菜籽油", "玉米油", "葵花籽油", "大豆油", "橄榄油", "猪油", "黄油",
    "芝麻油", "香油", "淀粉", "生粉", "番茄酱", "甜面酱", "黄豆酱", "柱侯酱",
    "叉烧酱", "芝麻酱", "腐乳", "豆豉", "蜂蜜",
    "八角", "桂皮", "香叶", "草果",
)

# 水和高汤会参与用量缩放与库存判断，但不应作为候选卡片上的“主要食材”
# 或“主要调味料”展示。
COOKING_AUXILIARIES = ("水", "清水", "热水", "开水", "高汤")

CONCRETE_SEASONING_TERMS = (
    "生抽", "老抽", "酱油", "蒸鱼豉油", "米醋", "陈醋", "香醋", "醋", "白砂糖",
    "白糖", "冰糖", "红糖", "盐", "料酒", "黄酒", "蚝油", "鱼露", "黑胡椒",
    "白胡椒", "胡椒粉", "花椒", "豆瓣酱", "辣椒酱", "番茄酱", "甜面酱", "黄豆酱",
    "味噌", "咖喱", "孜然", "五香粉", "十三香", "淀粉", "生粉", "香油", "芝麻酱",
)

ANIMAL_PROTEIN_GROUPS = (
    "牛肉", "猪肉", "鸡肉", "鸭肉", "羊肉", "鱼", "虾", "蟹", "贝类", "头足类",
)

# 兼容已有调用方；实际判断由类别完成，不再用“鸡”之类的裸子串误伤鸡精。
ANIMAL_PROTEIN_TERMS = tuple(dict.fromkeys(
    member for group in ANIMAL_PROTEIN_GROUPS for member in INGREDIENT_GROUPS[group]
))

STIR_FRY_MEAT_TERMS = tuple(dict.fromkeys(
    member for group in ("猪肉", "鸡肉", "牛肉", "鸭肉", "羊肉")
    for member in INGREDIENT_GROUPS[group]
))

STIR_FRY_VEGETABLE_TERMS = (
    "蔬菜", "木耳", "胡萝卜", "青椒", "笋", "土豆", "茄子", "西兰花", "青菜",
    "洋葱", "蘑菇", "香菇", "口蘑", "菜花", "白菜", "卷心菜", "豆角", "豆芽",
    "芹菜", "黄瓜", "丝瓜", "苦瓜", "冬瓜", "南瓜", "莲藕", "莴笋", "韭菜",
)

_DAILY_INGREDIENTS = (
    "番茄", "土豆", "胡萝卜", "白萝卜", "茄子", "黄瓜", "南瓜", "冬瓜", "丝瓜",
    "苦瓜", "莲藕", "莴笋", "芹菜", "韭菜", "洋葱", "西葫芦", "山药", "红薯",
    "芋头", "竹笋", "春笋", "冬笋", "香菜", "九层塔", "罗勒", "玉米", "豌豆",
    "毛豆", "花生", "鸡蛋", "鹌鹑蛋", "豆腐", "豆干", "腐竹", "粉丝", "粉条",
    "年糕", "馄饨", "饺子", "面粉", "面包糠", "可乐", "啤酒", "牛奶", "椰奶",
    "火锅底料", "高汤", "清水", "八角", "桂皮", "香叶", "草果", "花椒", "辣椒",
    "干辣椒", "小米辣", "泡椒", "剁椒", "葱", "姜", "蒜", "盐", "糖", "蜂蜜",
)

# 保留第一版解析器的稳定输出顺序，避免同一句库存因为词库扩充而改变
# cache key；新增词汇排在这些常用词之后。
_PARSER_PRIORITY = (
    "鸡蛋", "番茄", "面条", "青菜", "土豆", "胡萝卜", "米饭", "鸡翅", "鸡腿",
    "鸡胸肉", "排骨", "五花肉", "猪肉", "鸡肉", "牛腩", "鱼", "虾", "虾仁",
    "可乐", "咖喱", "牛肉", "肥牛", "牛排", "豆腐", "豆干", "茄子", "西兰花",
    "菜花", "蘑菇", "香菇", "口蘑", "平菇", "金针菇", "杏鲍菇", "洋葱", "玉米",
    "玉米粒", "葱", "姜", "蒜", "香菜", "八角", "冰糖", "白糖", "辣椒", "花生",
    "生抽", "老抽", "酱油", "料酒", "蚝油", "醋", "橄榄油", "食用油", "黄油",
    "黑胡椒", "海盐", "盐", "迷迭香",
)


def _all_known_terms() -> tuple[str, ...]:
    terms = set(_DAILY_INGREDIENTS)
    terms.update(INGREDIENT_NAME_ALIASES)
    terms.update(INGREDIENT_NAME_ALIASES.values())
    terms.update(INGREDIENT_GROUPS)
    terms.update(DIETARY_RESTRICTION_GROUPS)
    for members in INGREDIENT_GROUPS.values():
        terms.update(members)
    terms.update(CONCRETE_SEASONING_TERMS)
    terms.update(SEASONING_MARKERS)
    additions = sorted((term for term in terms if term), key=lambda item: (-len(item), item))
    return tuple(dict.fromkeys((*_PARSER_PRIORITY, *additions)))


KNOWN_INGREDIENTS = _all_known_terms()


def canonicalize_ingredient(name: str) -> str:
    value = re.sub(r"\s+", "", str(name or "").strip())
    return INGREDIENT_NAME_ALIASES.get(value, value)


def _labels_overlap(left: str, right: str) -> bool:
    if left == right:
        return True
    shorter, longer = sorted((left, right), key=len)
    # 单字子串极易误判，例如“鸡”/“鸡精”、“油”/“油菜”。单字关系必须
    # 通过显式类别表达。
    return len(shorter) >= 2 and shorter in longer


def _matching_groups(value: str, groups: Mapping[str, tuple[str, ...]]) -> set[str]:
    found: set[str] = set()
    for group, members in groups.items():
        canonical_group = canonicalize_ingredient(group)
        if _labels_overlap(value, canonical_group) or any(
            _labels_overlap(value, canonicalize_ingredient(member)) for member in members
        ):
            found.add(group)
    return found


def ingredient_matches(required: str, actual: str) -> bool:
    """判断具体食材或库存类别是否命中，不把类别关系当作替换指令。"""
    expected = canonicalize_ingredient(required)
    value = canonicalize_ingredient(actual)
    if not expected or not value:
        return False
    if _labels_overlap(expected, value):
        return True
    return bool(
        _matching_groups(expected, INGREDIENT_GROUPS)
        & _matching_groups(value, INGREDIENT_GROUPS)
    )


def ingredient_present(required: str, labels: Iterable[str]) -> bool:
    return any(ingredient_matches(required, label) for label in labels)


def restriction_matches(restriction: str, ingredient: str) -> bool:
    expected = canonicalize_ingredient(restriction)
    value = canonicalize_ingredient(ingredient)
    if not expected or not value:
        return False
    if _labels_overlap(expected, value):
        return True
    return bool(
        _matching_groups(expected, DIETARY_RESTRICTION_GROUPS)
        & _matching_groups(value, DIETARY_RESTRICTION_GROUPS)
    )


def is_seasoning(label: str) -> bool:
    value = canonicalize_ingredient(label)
    return bool(value) and any(marker in value for marker in SEASONING_MARKERS)


def split_main_foods_and_seasonings(labels: Iterable[str]) -> tuple[list[str], list[str]]:
    foods: list[str] = []
    seasonings: list[str] = []
    for label in labels:
        value = str(label).strip()
        if not value:
            continue
        if canonicalize_ingredient(value) in COOKING_AUXILIARIES:
            continue
        target = seasonings if is_seasoning(value) else foods
        if value not in target:
            target.append(value)
    return foods or seasonings, seasonings


def is_animal_protein(name: str) -> bool:
    value = canonicalize_ingredient(name)
    if not value:
        return False
    groups = _matching_groups(value, INGREDIENT_GROUPS)
    return any(group in groups for group in ANIMAL_PROTEIN_GROUPS)


def extract_known_ingredients(text: str) -> list[str]:
    """按出现位置提取日常食材，优先较长词并合并无损同义词。"""
    source = str(text or "")
    matches: list[tuple[int, int, str]] = []
    for term in sorted(KNOWN_INGREDIENTS, key=lambda item: (-len(item), item)):
        for match in re.finditer(re.escape(term), source):
            matches.append((match.start(), match.end(), canonicalize_ingredient(term)))
    matches.sort(key=lambda item: (item[0], -(item[1] - item[0])))

    occupied: list[tuple[int, int]] = []
    found: list[str] = []
    for start, end, canonical in matches:
        if any(start < used_end and end > used_start for used_start, used_end in occupied):
            continue
        occupied.append((start, end))
        if canonical and canonical not in found:
            found.append(canonical)
    priority: dict[str, int] = {}
    for index, term in enumerate(KNOWN_INGREDIENTS):
        priority.setdefault(canonicalize_ingredient(term), index)
    return sorted(found, key=lambda item: priority.get(item, len(priority)))
