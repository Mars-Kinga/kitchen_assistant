#!/usr/bin/env python3
"""One-time, offline migration for beginner-readable local recipes."""

from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "skills/kitchen_assistant/recipes/catalog"
SEASONINGS = ("油", "盐", "生抽", "老抽", "醋", "糖", "胡椒", "料酒", "淀粉", "水", "姜", "葱", "蒜", "酱")
PROTEINS = ("鸡", "鸭", "牛", "羊", "猪", "肉", "排骨", "里脊", "鱼", "虾", "蟹", "鱿鱼", "花甲", "扇贝", "生蚝", "鸡蛋", "豆腐")
TITLE_PROTEINS = (
    (("鸡肉", "鸡胸", "鸡腿", "鸡丁", "鸡丝", "鸡块", "鸡扒", "鸡翅", "鸡爪", "鸡胗", "鸡粒"), "鸡肉", "300 克"),
    (("排骨",), "排骨", "400 克"),
    (("牛肉", "牛腩", "牛柳", "牛腱", "肥牛"), "牛肉", "300 克"),
    (("羊肉", "羊排"), "羊肉", "300 克"),
    (("鸭",), "鸭肉", "400 克"),
    (("猪肉", "肉末", "肉丝", "肉片", "里脊", "猪蹄", "猪脚", "猪肘", "肘子", "五花肉"), "猪肉", "300 克"),
    (("虾",), "虾仁", "250 克"),
    (("鱿鱼",), "鱿鱼", "300 克"),
    (("花甲", "蛤蜊"), "花甲", "500 克"),
    (("扇贝",), "扇贝", "300 克"),
    (("生蚝",), "生蚝", "500 克"),
    (("螃蟹", "蟹"), "螃蟹", "500 克"),
    (("鱼",), "鱼", "400 克"),
    (("豆腐",), "豆腐", "300 克"),
    (("鸡蛋", "炒蛋", "蛋花", "蒸蛋"), "鸡蛋", "2 个"),
)
TITLE_PRODUCE = (
    "油麦菜", "娃娃菜", "小白菜", "空心菜", "西兰花", "荷兰豆", "四季豆", "酸豆角",
    "金针菇", "杏鲍菇", "蟹味菇", "菜花", "花菜", "菜心", "生菜", "芥蓝", "菠菜",
    "白菜", "包菜", "豆角", "豇豆", "茄子", "土豆", "莲藕", "青椒", "番茄", "西葫芦",
    "香菇", "木耳", "山药", "芹菜", "芦笋", "冬瓜", "南瓜", "萝卜", "玉米", "海带",
    "紫菜", "韭菜", "酸菜", "榨菜", "竹笋", "黄瓜", "苦瓜", "丝瓜", "毛豆", "豌豆",
)


def amount(row: dict) -> str:
    return f"{row.get('amount', '')}{row.get('unit') or ''}".replace("  ", " ").strip()


def core_ingredients(recipe: dict) -> list[dict]:
    rows = recipe.get("ingredients", [])
    core = [r for r in rows if not any(x in str(r.get("name", "")) for x in SEASONINGS)]
    return core or rows[:2]


def label(row: dict) -> str:
    return f"{amount(row)}{row.get('name', '')}"


def ensure_title_proteins(recipe: dict) -> bool:
    """Make title-promised proteins real ingredients; remove conflicting generic meat."""
    title = str(recipe.get("name", ""))
    names = [str(x.get("name", "")) for x in recipe.get("ingredients", [])]
    changed = False
    for triggers, canonical, qty in TITLE_PROTEINS:
        if canonical == "鱼" and "鱼香" in title:
            continue
        if not any(trigger in title for trigger in triggers):
            continue
        accepted = {
            "鸡肉": ("鸡肉", "鸡胸", "鸡腿", "鸡丁", "鸡丝", "鸡块"),
            "猪肉": ("猪", "五花肉", "里脊", "肉末", "肉丝", "肉片"),
            "牛肉": ("牛",), "羊肉": ("羊",), "鸭肉": ("鸭",),
            "鱼": ("鱼",), "虾仁": ("虾",), "螃蟹": ("蟹",),
        }.get(canonical, (canonical,))
        if any(any(word in item for word in accepted) for item in names):
            continue
        if canonical in {"鸡肉", "牛肉", "羊肉", "鸭肉", "猪肉"} and str(recipe.get("recipe_id", "")).startswith("local_expansion_"):
            other = ("鸡肉", "鸡腿肉", "猪肉", "牛肉", "羊肉", "鸭肉")
            recipe["ingredients"] = [x for x in recipe["ingredients"] if str(x.get("name", "")) not in other]
        recipe["ingredients"].insert(0, {"name": canonical, "amount": qty})
        names.insert(0, canonical)
        changed = True
    return changed


def ensure_title_produce(recipe: dict) -> bool:
    title = str(recipe.get("name", ""))
    names = [str(x.get("name", "")) for x in recipe.get("ingredients", [])]
    found = [item for item in TITLE_PRODUCE if item in title and not any(item in name for name in names)]
    if not found:
        return False
    if str(recipe.get("recipe_id", "")).startswith("local_expansion_"):
        recipe["ingredients"] = [x for x in recipe["ingredients"] if x.get("name") != "时令蔬菜"]
    for item in reversed(found):
        qty = "10 克" if item in {"紫菜"} else "200 克"
        recipe["ingredients"].insert(0, {"name": item, "amount": qty})
    return True


def method(name: str) -> str:
    if any(x in name for x in ("汤", "煲", "炖", "焖", "烧", "卤")):
        return "simmer"
    if "粥" in name:
        return "porridge"
    if any(x in name for x in ("蒸", "粉蒸")):
        return "steam"
    if any(x in name for x in ("凉拌", "沙拉", "拌")) and "拌饭" not in name:
        return "cold"
    if any(x in name for x in ("烤", "焗")):
        return "bake"
    if any(x in name for x in ("包子", "小笼包", "灌汤包", "水煎包", "生煎包", "烧麦", "饺", "馄饨", "馅饼", "盒子", "灌饼")):
        return "dough"
    if any(x in name for x in ("煎", "炸", "椒盐")):
        return "panfry"
    if any(x in name for x in ("面", "粉", "米线", "馄饨", "饺")):
        return "noodle"
    if any(x in name for x in ("饭", "盖浇", "抓饭")):
        return "rice"
    return "stir"


def heat_step(text: str, seconds: int, heat: str, safety: str | None = None) -> dict:
    row = {"instruction": text, "duration_seconds": seconds, "heat_level": heat}
    if safety:
        row["safety_note"] = safety
    return row


def build_steps(recipe: dict) -> list[dict]:
    name = recipe["name"]
    rows = core_ingredients(recipe)
    main = rows[0]
    rest = rows[1:]
    main_text = label(main)
    rest_text = "、".join(label(x) for x in rest) or "配菜"
    animal = any(
        any(x in str(row.get("name", "")) for x in PROTEINS[:-2])
        for row in rows
    )
    safety = "切开最厚处检查，动物性食材中心不得有粉红或透明部分。" if animal else None
    prep = {
        "instruction": f"按清单称量食材。将{main.get('name')}处理成约 2 厘米的均匀块或片，{rest_text}分别清理后切好。",
    }
    if animal:
        prep["safety_note"] = "生肉和水产不要在水槽中冲洗；处理后清洗双手、刀具、砧板和台面。"
    kind = method(name)
    if kind == "simmer":
        return [
            prep,
            {"instruction": f"锅中加入清单中的食用油，中火加热 30 秒；放入葱姜蒜炒约 30 秒，闻到香味即可。"},
            heat_step(f"加入{main_text}，中火翻炒 4 分钟，使各面均匀受热。", 240, "中火", safety),
            {"instruction": f"加入{rest_text}和清单中的清水、生抽等调味料，翻匀后大火煮至沸腾。"},
            heat_step(f"转小火加盖炖煮 20 分钟；中途翻动一次，避免锅底粘连。", 1200, "小火"),
            {"instruction": "开盖检查主料熟度和配菜软硬；未达到状态时继续加热 5 分钟再检查。", "safety_note": safety or "不要只凭计时判断，确认食材达到可食用状态。"},
            heat_step(f"加入清单中的盐调味，中火收汁 2 分钟，汤汁达到菜名“{name}”需要的浓度后关火。", 120, "中火"),
        ]
    if kind == "steam":
        return [
            prep,
            {"instruction": f"将{main_text}与清单中的生抽、盐和淀粉拌匀，静置 10 分钟入味。"},
            {"instruction": f"把{rest_text}平铺在耐热盘底，再将主料单层铺在上面，不要堆成厚团。"},
            heat_step("蒸锅加入足量清水，大火烧至持续冒蒸汽后再放入耐热盘。", 180, "大火"),
            heat_step(f"加盖中火蒸 12 分钟，期间不要频繁开盖。", 720, "中火", "开盖时让蒸汽远离面部和手臂。"),
            {"instruction": "切开最厚的一块检查中心；未熟时重新加盖蒸 3 分钟后再次检查。", "safety_note": safety or "确认中心完全熟透后再食用。"},
            {"instruction": f"确认熟透后关火，戴隔热手套取出，静置 1 分钟再食用{name}。"},
        ]
    if kind == "cold":
        return [
            prep,
            {"instruction": f"需要熟制的{main.get('name')}放入沸水或平底锅中加热，至中心完全熟透后取出。", "safety_note": safety or "确认需要熟制的食材已熟，不把生食与熟食混放。"},
            {"instruction": f"将熟制后的主料摊开放凉 3 分钟；{rest_text}充分沥干，避免稀释调味汁。"},
            {"instruction": "小碗中加入清单里的油、醋、生抽、盐等调味料，搅拌至盐糖溶解。"},
            {"instruction": f"沙拉碗中放入{main_text}和{rest_text}，先淋入一半调味汁，从碗底向上翻拌。"},
            {"instruction": "检查咸淡后加入剩余调味汁，再翻拌约 20 秒；拌好后立即食用或盖好冷藏。"},
        ]
    if kind == "bake":
        return [
            prep,
            {"instruction": f"将{main_text}与清单中的盐、生抽和香辛料拌匀，腌制 15 分钟。"},
            heat_step("烤箱按菜谱温度预热 10 分钟，烤盘铺烘焙纸。", 600, "上下火 200℃"),
            {"instruction": f"把主料和{rest_text}单层摆入烤盘，彼此留出空隙，表面薄薄刷油。"},
            heat_step("放入烤箱中层烤 15 分钟，再取出翻面。", 900, "上下火 200℃", "取放烤盘时使用隔热手套。"),
            heat_step("继续烤 8 分钟，直到表面上色。", 480, "上下火 200℃", safety),
            {"instruction": "检查最厚处熟度；达到要求后静置 3 分钟再切分装盘。", "safety_note": safety or "确认中心熟透后再食用。"},
        ]
    if kind == "panfry":
        return [
            prep,
            {"instruction": f"用厨房纸吸干{main.get('name')}表面水分，加入清单中的盐和调味料拌匀，静置 10 分钟。"},
            heat_step("平底锅加入清单中的食用油，中火加热 30 秒。", 30, "中火"),
            heat_step(f"放入{main_text}单层铺开，先不要移动，煎 3 分钟至底面定型。", 180, "中火"),
            heat_step(f"翻面后加入{rest_text}，继续煎炒 3 分钟，使各面均匀上色。", 180, "中火", safety),
            {"instruction": "检查最大一块的中心状态；未熟时转小火每次续煎 1 分钟并再次检查。", "safety_note": safety or "不要只凭表面颜色判断熟度。"},
            {"instruction": f"达到熟度后关火，静置 2 分钟再装盘，完成{name}。"},
        ]
    if kind == "dough":
        return [
            prep,
            {"instruction": "按清单称量面粉和水，分次加水搅成面絮，再揉成表面基本光滑的面团。"},
            {"instruction": "面团盖好醒 20 分钟；等待时将主料和配菜切碎，加入清单中的盐、生抽等顺一个方向拌成馅。", "duration_seconds": 1200},
            {"instruction": "把面团搓成长条并等分，逐个擀成中间略厚、边缘较薄的面皮。"},
            {"instruction": "每张面皮中央放馅，边缘捏紧；成品大小保持一致，避免熟制时间差异过大。"},
            heat_step("按菜名采用蒸、煮或煎的方式熟制 10 分钟，保持稳定火力。", 600, "中火", "含肉馅的成品应切开一个，确认中心完全熟透。"),
            {"instruction": f"取一个{name}检查面皮和馅心；未熟时继续加热 2 分钟再检查，熟透后再装盘。"},
        ]
    if kind in {"noodle", "porridge", "rice"}:
        return [
            prep,
            {"instruction": f"先按包装或清单要求把主食煮至熟透；煮好后沥水或焖至水分被吸收。"},
            heat_step(f"锅中加入清单中的食用油，中火加热 30 秒，放入{main_text}翻炒 3 分钟。", 210, "中火", safety),
            heat_step(f"加入{rest_text}继续翻炒或煮 3 分钟，至配菜断生。", 180, "中火"),
            {"instruction": "加入清单中的生抽、盐等调味料，翻拌均匀；汤面或粥类按清单补足清水。"},
            heat_step("加入已熟主食，中火加热 2 分钟，使主食与配料温度、味道均匀。", 120, "中火"),
            {"instruction": "检查动物性食材熟度和主食软硬，达到要求后关火盛出。", "safety_note": safety or "成品中心应充分热透。"},
        ]
    return [
        prep,
        {"instruction": f"将{main_text}加入清单中的生抽、淀粉或盐拌匀，静置 10 分钟；蔬菜类无需腌制。"},
        heat_step("炒锅加入清单中的食用油，中火加热 30 秒，放入葱姜蒜炒香。", 60, "中火"),
        heat_step(f"加入{main_text}，铺开后翻炒 3 分钟，使表面均匀受热。", 180, "中火", safety),
        heat_step(f"加入{rest_text}，转大火翻炒 2 分钟，至蔬菜断生或配料热透。", 120, "大火"),
        {"instruction": "沿锅边加入清单中的生抽、盐等调味料，快速翻匀；锅底太干时加入清单中的清水。"},
        {"instruction": "检查最大一块主料的中心状态，确认达到熟度后关火装盘。", "safety_note": safety or "以食材实际状态为准，不只依赖计时。"},
    ]


def main() -> None:
    changed = 0
    for path in sorted(CATALOG.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        for recipe in payload.get("recipes", []):
            for step in recipe.get("steps", []):
                text = str(step.get("instruction", ""))
                if "洗净" in text and any(word in text for word in ("排骨", "牛肉", "鸡肉")):
                    step["instruction"] = text.replace("洗净", "清理后")
            ingredients_changed = ensure_title_proteins(recipe)
            ingredients_changed = ensure_title_produce(recipe) or ingredients_changed
            if ingredients_changed:
                recipe["steps"] = build_steps(recipe)
                recipe["ingredient_review_version"] = 1
                changed += 1
                continue
            if recipe.get("beginner_review_version") == 1:
                continue
            recipe["steps"] = build_steps(recipe)
            recipe["beginner_review_version"] = 1
            recipe["estimated_time_minutes"] = max(int(recipe.get("estimated_time_minutes", 20)), 20)
            changed += 1
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"离线校订完成：更新 {changed} 道菜谱。")


if __name__ == "__main__":
    main()
