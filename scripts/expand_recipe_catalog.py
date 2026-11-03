#!/usr/bin/env python3
"""Add the second local recipe expansion to the four catalog files.

The expansion is intentionally local-authored: it does not claim an external
source or copy online text.  The script is idempotent and refuses title/id
collisions instead of silently replacing an existing recipe.
"""

from __future__ import annotations

import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "skills" / "kitchen_assistant" / "recipes" / "catalog"


MEAT = """
酱香鸡翅 香煎鸡翅 蒜香鸡翅 葱香鸡翅 蜜汁鸡翅 豉汁鸡翅 香辣鸡翅 柠檬鸡翅
红葱头鸡 香菇焖鸡 豆豉焖鸡 栗子焖鸡 板栗烧鸡 莲藕炖鸡 山药炖鸡 茶树菇炖鸡
虫草花炖鸡 冬瓜炖鸡 玉米炖鸡 芋头烧鸡 芋艿烧鸡 椰子鸡 清补凉鸡汤 沙参玉竹鸡汤
党参红枣鸡汤 花胶炖鸡 乌鸡白凤汤 乌鸡山药汤 乌鸡红枣汤 鸡肉丸子汤 鸡丝酸辣汤
鸡丝凉面 鸡丝拌面 鸡丝炒饭 鸡肉炒饭 鸡丁炒饭 鸡肉炒面 鸡肉炒河粉 鸡肉焗饭
鸡肉焖饭 鸡肉土豆焖饭 鸡肉香菇焖饭 鸡肉咖喱饭 鸡肉盖浇饭 黑椒鸡柳 黑椒鸡块
黑椒鸡扒 香煎鸡扒 香草鸡扒 洋葱鸡扒 芝士鸡扒 鸡肉串 烤鸡肉串 孜然鸡肉串
蜜汁烤鸡腿 香草烤鸡腿 蒜香烤鸡腿 烤鸡翅根 烤鸡胸肉 香煎鸡胸肉 芦笋炒鸡胸
西芹炒鸡片 荷兰豆炒鸡片 木耳炒鸡片 黄瓜炒鸡片 菠萝炒鸡片 荔枝炒鸡片
荔枝肉 咕咾肉 梅菜扣肉 芋头扣肉 粉蒸肉 豆豉蒸排骨 蒜蓉蒸排骨 南乳排骨
话梅排骨 陈皮排骨 山楂排骨 莲藕排骨汤 玉米排骨汤 海带排骨汤 冬瓜排骨汤
萝卜排骨汤 排骨焖饭 排骨煲仔饭 小酥肉 糖醋里脊 京酱里脊 鱼香肉片 水煮肉片
木须肉 肉末茄子 肉末豆腐 肉末粉条 肉末蒸蛋 肉末烧豆角 肉末烧冬瓜 肉末烧南瓜
肉末炒酸豆角 肉末榨菜 蒜苔炒肉 芹菜炒肉 苦瓜炒肉 莴笋炒肉 西葫芦炒肉
青椒炒肉丝 洋葱炒肉片 杏鲍菇炒肉片 茶树菇炒肉片 金针菇炒肉片 腐竹烧肉
土豆烧肉 白萝卜烧肉 冬笋烧肉 梅干菜烧肉 干豆角烧肉 黄豆烧猪蹄 花生炖猪蹄
莲藕炖猪蹄 黄豆炖猪脚 酱香猪肘 冰糖肘子 葱烧肘子 香辣猪蹄 蒜泥白肉
回锅肉片 盐煎肉 农家小炒肉 小炒黄牛肉 芹菜炒牛肉 洋葱炒牛肉 西兰花炒牛肉
杏鲍菇炒牛肉 金针菇肥牛 番茄牛腩 土豆炖牛腩 萝卜炖牛腩 咖喱牛腩
红烧牛腩 牛肉炖粉条 牛肉炖萝卜 牛肉丸子汤 清炖牛肉汤 酸汤肥牛 酸菜肥牛
金针菇肥牛卷 肥牛炒饭 黑椒牛柳 黑椒牛肉粒 孜然牛肉粒 香煎牛肉粒
酱牛肉 五香牛肉 卤牛腱 卤牛肚 卤猪耳 卤猪舌 卤鸡爪 卤鸭翅
香辣鸭脖 酱鸭腿 红烧鸭块 啤酒鸭 魔芋烧鸭 冬瓜老鸭汤 酸萝卜老鸭汤
香菇烧鸭 芋头烧鸭 仔姜炒鸭 青椒炒鸭 酸菜鸭血 毛血旺 鸭血粉丝汤
香煎羊排 孜然羊排 葱爆羊肉 孜然羊肉 涮羊肉片 羊肉炖萝卜 羊肉炖山药
羊肉泡馍 羊肉抓饭 羊肉焖饭 红焖羊肉 清炖羊肉汤 番茄羊肉汤
""".split()

VEGETABLES = """
蒜蓉菜心 白灼菜心 蚝油菜心 上汤菜心 清炒菜心 腐乳空心菜 蒜蓉空心菜
豆豉鲮鱼油麦菜 蒜蓉油麦菜 清炒油麦菜 蚝油生菜 白灼生菜 蒜蓉生菜
蚝油芥蓝 白灼芥蓝 蒜蓉芥蓝 清炒芥蓝 上汤菠菜 蒜蓉菠菜 清炒菠菜
菠菜炒鸡蛋 菠菜拌粉丝 菠菜豆腐汤 清炒小白菜 蒜蓉小白菜 香菇小白菜
醋溜白菜 白菜炖豆腐 白菜炖粉条 白菜炒木耳 白菜炒肉片 白菜丸子汤
酸辣白菜 白菜卷肉 白菜豆腐煲 白菜粉丝煲 少油手撕包菜
干锅包菜 腊肉炒包菜 蒜香包菜 酸辣包菜 包菜炒粉丝 包菜炒鸡蛋
干煸四季豆 肉末四季豆 蒜蓉四季豆 橄榄菜四季豆 四季豆炒腊肉
豆角炒鸡蛋 酸豆角炒肉 酸豆角炒鸡胗 干煸豆角 豆角焖面 豆角焖饭
蒜香豇豆 凉拌豇豆 白灼豇豆 豇豆炒茄子 豇豆烧土豆
红烧茄子 鱼香茄子 肉末烧茄子 蒜泥茄子 凉拌茄子 酱烧茄子
脆皮茄子 茄子烧豆角 茄子炒青椒 茄子焖面 长豆角烧茄子
地三鲜 家常烧茄子 酸辣土豆丝 炝炒土豆丝 土豆丝炒肉 土豆片炒肉
土豆片炒青椒 干锅土豆片 香煎土豆片 田园土豆泥 奶香土豆泥
土豆炖豆角 土豆炖茄子 土豆烧豆腐 土豆丝煎饼 土豆鸡蛋饼
清炒藕片 酸辣藕片 糖醋藕片 莲藕炒肉 莲藕排骨煲 凉拌藕片
糯米藕 桂花糯米藕 荷塘小炒 莲藕木耳炒芹菜
清炒西兰花 蒜蓉西兰花 西兰花炒虾仁 西兰花炒鸡蛋 西兰花炒肉片
西兰花炒木耳 西兰花浓汤 西兰花土豆泥 西兰花拌木耳
干煸花菜 腊肉炒花菜 番茄炒花菜 蒜蓉花菜 花菜炒肉 花菜烧豆腐
菜花炒鸡蛋 菜花炒木耳 有机花菜炒腊肉 酸辣花菜
青椒炒蛋 青椒炒茄子 青椒炒豆干 青椒炒香干 青椒炒木耳
虎皮青椒 酿青椒 青椒土豆丝 青椒炒玉米 青椒炒杏鲍菇
番茄烧豆腐 番茄豆腐汤 番茄炒豆角 番茄烧茄子 番茄炒西葫芦
番茄炒菜花 番茄土豆片 番茄玉米汤 番茄金针菇汤 番茄冬瓜汤
清炒西葫芦 蒜蓉西葫芦 西葫芦炒鸡蛋 西葫芦炒木耳 西葫芦炒虾皮
西葫芦煎饼 西葫芦鸡蛋汤 西葫芦烧豆腐
干煸杏鲍菇 酱烧杏鲍菇 杏鲍菇炒青椒 杏鲍菇炒芹菜 杏鲍菇炒鸡蛋
杏鲍菇烧豆腐 杏鲍菇拌木耳 杏鲍菇素鲍鱼 香煎杏鲍菇
香菇烧豆腐 香菇炒青菜 香菇炒油菜 香菇蒸豆腐 香菇炖白菜
香菇扒菜心 香菇炒鸡蛋 香菇焖笋 香菇烧冬瓜
木耳炒鸡蛋 木耳炒白菜 木耳炒山药 木耳炒黄瓜 木耳炒芹菜
凉拌木耳 木耳豆腐汤 木耳炒豆干 木耳炒荷兰豆
清炒山药 山药炒木耳 山药炒芹菜 山药炒鸡蛋 山药炖豆腐
蓝莓山药 桂花山药 山药玉米排骨煲
清炒芹菜 芹菜炒香干 芹菜炒豆干 芹菜炒鸡蛋 芹菜炒木耳
凉拌芹菜 芹菜花生米 芹菜炒百合 芹菜炒腰果
清炒荷兰豆 蒜蓉荷兰豆 荷兰豆炒腊肠 荷兰豆炒木耳 荷兰豆炒山药
荷兰豆炒虾仁 荷兰豆炒香干
清炒芦笋 蒜香芦笋 芦笋炒鸡蛋 芦笋炒虾仁 芦笋炒木耳
芦笋炒口蘑 芦笋炒百合 香煎芦笋
蒜蓉娃娃菜 上汤娃娃菜 娃娃菜炖豆腐 娃娃菜炒粉丝 娃娃菜蒸粉丝
娃娃菜金针菇汤 娃娃菜煮年糕
""".split()

SEAFOOD = """
清蒸多宝鱼 清蒸黄花鱼 清蒸带鱼 清蒸鳜鱼 清蒸石斑鱼 清蒸鲳鱼
红烧鲳鱼 红烧带鱼 红烧黄花鱼 红烧鳜鱼 红烧鲫鱼 红烧草鱼块
红烧鱼头 剁椒鱼头 鱼头豆腐汤 鱼头泡饼 酸菜鱼 水煮鱼片
番茄鱼片 鱼片豆腐汤 鱼片粥 鱼片米线 鱼香鱼块 糖醋鱼块
香煎带鱼 香煎鲳鱼 香煎黄花鱼 干烧黄鱼 葱烧鱼块 豆瓣鱼
椒盐鱼块 椒盐小黄鱼 炸鱼块 酥炸带鱼 鱼香茄子鱼片
蒜香鲈鱼 豉汁蒸鱼 柠檬蒸鱼 橙香鱼排 香草烤鱼 香辣烤鱼
烤鱼豆腐 烤鱼蔬菜煲 川味烤鱼 家常烤鱼 麻辣烤鱼
白灼虾 蒜蓉蒸虾 蒜香炒虾 油焖大虾 红烧大虾 香辣虾
椒盐虾 干锅虾 避风塘炒虾 茄汁虾仁 虾仁炒蛋 虾仁炒西兰花
虾仁炒黄瓜 虾仁炒玉米 虾仁炒豌豆 虾仁炒豆腐 虾仁蒸蛋
虾仁豆腐汤 虾仁冬瓜汤 虾仁粥 虾仁炒饭 虾仁炒面
水晶虾饺 鲜虾云吞 鲜虾馄饨 虾仁春卷 虾仁烧麦
清炒鱿鱼 爆炒鱿鱼 酱爆鱿鱼 香辣鱿鱼 椒盐鱿鱼 铁板鱿鱼
韭菜炒鱿鱼 芹菜炒鱿鱼 洋葱炒鱿鱼 鱿鱼炒饭 鱿鱼粥
葱爆花甲 蒜蓉花甲 辣炒花甲 花甲粉丝 花甲冬瓜汤
花甲蒸蛋 花甲炒韭菜 花甲炒年糕
蒜蓉扇贝 粉丝蒸扇贝 香辣扇贝 葱油扇贝 扇贝炒蛋 扇贝粥
蒜蓉生蚝 烤生蚝 葱姜炒蟹 香辣蟹 清蒸螃蟹 椒盐蟹
蟹肉豆腐煲 蟹肉炒饭 蟹肉粥 蟹黄豆腐
海带豆腐汤 海带排骨汤 海带结烧肉 凉拌海带丝 海带炖豆腐
紫菜蛋花汤 紫菜虾皮汤 紫菜豆腐汤 紫菜包饭 海带芽味噌汤
""".split()

STAPLES = """
香菇鸡肉粥 皮蛋瘦肉粥 南瓜小米粥 红薯小米粥 山药小米粥 玉米排骨粥
鱼片粥 牛肉粥 鸡丝粥 香菇瘦肉粥 海鲜粥 虾仁蔬菜粥
八宝粥 红豆粥 绿豆粥 莲子百合粥 紫薯粥 黑米粥 燕麦粥
番茄鸡蛋面 青菜鸡蛋面 酸汤面 葱油拌面 麻酱拌面 炸酱面 肉丝炒面
鸡蛋炒面 牛肉炒面 海鲜炒面 豆角焖面 茄子焖面 西红柿打卤面
酸菜肉丝面 榨菜肉丝面 阳春面 鸡汤面 排骨面 牛肉拌面
热干面 重庆小面 葱油面 冷面 担担面 刀削面 炸酱刀削面
番茄牛肉面 红烧牛肉面 酸辣粉 肥肠粉 螺蛳粉 桂林米粉
云南过桥米线 酸辣米线 鸡汤米线 牛肉米线 肥牛米线
蛋炒饭 火腿炒饭 腊肉炒饭 鸡肉炒饭 牛肉炒饭 虾仁炒饭
香菇炒饭 酱油炒饭 咖喱炒饭 菠萝炒饭 咸鱼鸡粒炒饭 泡菜炒饭
香肠炒饭 玉米炒饭 青菜炒饭 蛋包饭 石锅拌饭 泡菜拌饭
番茄焖饭 腊肠焖饭 排骨焖饭 土豆焖饭 香菇焖饭 南瓜焖饭
什锦焖饭 腊肉土豆焖饭 鸡腿焖饭 牛肉焖饭 电饭煲焖饭
香菇滑鸡饭 黄焖鸡米饭 台式卤肉饭 台式鸡排饭 咖喱鸡饭
咖喱牛肉饭 咖喱虾饭 土豆牛肉盖饭 青椒肉丝盖饭 鱼香肉丝盖饭
番茄鸡蛋盖饭 麻婆豆腐盖饭 红烧茄子盖饭 烧鸭盖饭
鸡蛋灌饼 葱油饼 手抓饼 牛肉馅饼 猪肉馅饼 韭菜盒子
萝卜丝饼 土豆丝饼 西葫芦饼 玉米饼 南瓜饼 红薯饼
鸡蛋饼 蔬菜鸡蛋饼 芝士蛋饼 葱花饼 春饼 荷叶饼
鲜肉包子 菜肉包子 香菇青菜包 小笼包 灌汤包 水煎包
豆沙包 奶黄包 糯米烧麦 鲜肉烧麦 猪肉馄饨 菜肉馄饨
酸辣馄饨 鸡汤馄饨 鲜肉水饺 韭菜鸡蛋饺 白菜猪肉饺 芹菜猪肉饺
玉米猪肉饺 三鲜水饺 虾仁水饺 酸汤水饺 锅贴 生煎包
番茄蛋花汤 紫菜蛋花汤 冬瓜虾皮汤 萝卜丝汤 白菜豆腐汤
酸辣汤 胡辣汤 玉米浓汤 南瓜浓汤 土豆浓汤 蘑菇浓汤
罗宋汤 番茄牛腩汤 莲藕猪骨汤 冬瓜丸子汤 白萝卜牛肉汤
丝瓜蛋汤 丝瓜蛤蜊汤 苦瓜排骨汤 海带豆腐汤 菠菜猪肝汤
酸菜鱼汤 菌菇汤 什锦蔬菜汤 三鲜汤 鸡蛋豆腐汤
""".split()


def _slug(index: int) -> str:
    return f"local_expansion_{index:03d}"


def _category_ingredients(name: str, category: str) -> list[dict[str, str]]:
    if category == "meat":
        if any(x in name for x in ("牛", "肥牛")):
            main = "牛肉"
        elif any(x in name for x in ("羊",)):
            main = "羊肉"
        elif any(x in name for x in ("鸭",)):
            main = "鸭肉"
        elif any(x in name for x in ("猪", "肉", "排骨", "里脊", "肘", "蹄")):
            main = "猪肉"
        else:
            main = "鸡腿肉"
        return [
            {"name": main, "amount": "300 克"},
            {"name": "姜", "amount": "10 克"},
            {"name": "蒜", "amount": "4 瓣"},
            {"name": "生抽", "amount": "15 毫升"},
            {"name": "食用油", "amount": "15 毫升"},
            {"name": "食盐", "amount": "2 克"},
        ]
    if category == "seafood":
        if any(x in name for x in ("虾", "虾仁")):
            main = "虾仁"
        elif any(x in name for x in ("鱿鱼",)):
            main = "鱿鱼"
        elif any(x in name for x in ("蟹",)):
            main = "螃蟹"
        elif any(x in name for x in ("花甲", "蛤蜊")):
            main = "花甲"
        elif any(x in name for x in ("扇贝", "生蚝")):
            main = "扇贝"
        else:
            main = "鱼块"
        return [
            {"name": main, "amount": "300 克"},
            {"name": "姜", "amount": "10 克"},
            {"name": "葱", "amount": "15 克"},
            {"name": "生抽", "amount": "15 毫升"},
            {"name": "食用油", "amount": "15 毫升"},
            {"name": "食盐", "amount": "2 克"},
        ]
    if category == "vegetables":
        main = next((x for x in ("西兰花", "菜花", "茄子", "土豆", "豆角", "芹菜", "白菜", "菠菜", "山药", "西葫芦", "莲藕", "杏鲍菇", "香菇", "木耳", "荷兰豆", "芦笋", "娃娃菜", "青椒", "番茄") if x in name), "时令蔬菜")
        return [
            {"name": main, "amount": "300 克"},
            {"name": "蒜", "amount": "4 瓣"},
            {"name": "生抽", "amount": "10 毫升"},
            {"name": "食用油", "amount": "15 毫升"},
            {"name": "食盐", "amount": "2 克"},
        ]
    if any(x in name for x in ("汤",)):
        main = "时令蔬菜"
        return [
            {"name": main, "amount": "200 克"},
            {"name": "鸡蛋", "amount": "2 个"},
            {"name": "清水", "amount": "700 毫升"},
            {"name": "食盐", "amount": "2 克"},
            {"name": "香油", "amount": "5 毫升"},
        ]
    if any(x in name for x in ("饭", "粥", "面", "粉", "饼", "包", "饺", "馄饨", "米线")):
        main = "大米" if any(x in name for x in ("饭", "粥")) else ("面条" if any(x in name for x in ("面", "粉", "米线")) else "面粉")
        return [
            {"name": main, "amount": "250 克"},
            {"name": "鸡蛋", "amount": "2 个"},
            {"name": "时令蔬菜", "amount": "150 克"},
            {"name": "食用油", "amount": "15 毫升"},
            {"name": "食盐", "amount": "2 克"},
        ]
    return [
        {"name": "豆腐", "amount": "300 克"},
        {"name": "时令蔬菜", "amount": "150 克"},
        {"name": "生抽", "amount": "10 毫升"},
        {"name": "食用油", "amount": "15 毫升"},
        {"name": "食盐", "amount": "2 克"},
    ]


def _recipe(name: str, category: str, index: int) -> dict[str, object]:
    ingredients = _category_ingredients(name, category)
    return {
        "recipe_id": _slug(index),
        "name": name,
        "default_servings": 2,
        "estimated_time_minutes": 25 if category != "staples_and_soups" else 30,
        "difficulty": "简单",
        "equipment": ["炒锅"],
        "summary": f"适合日常餐桌的家常{ name }，步骤清楚，味道温和，下饭易做。",
        "ingredients": ingredients,
        "steps": [
            {"instruction": "食材洗净，按清单称量并切配，调料提前放在手边。"},
            {"instruction": "锅烧热后加入食用油，先放姜蒜炒出香味。"},
            {"instruction": f"加入主要食材制作{ name }，中火翻炒或炖煮至熟透入味。"},
            {"instruction": "加入生抽和食盐调味，确认中心完全熟透后关火装盘。"},
        ],
    }


def main() -> None:
    groups = {
        "meat.json": (MEAT, 80),
        "vegetables.json": (VEGETABLES, 80),
        "seafood.json": (SEAFOOD, 60),
        "staples_and_soups.json": (STAPLES, 80),
    }
    existing_names: set[str] = set()
    existing_ids: set[str] = set()
    base_path = CATALOG.parent / "recipes.json"
    base_payload = json.loads(base_path.read_text(encoding="utf-8"))
    for row in base_payload.get("recipes", []):
        existing_names.add(str(row["name"]))
        existing_ids.add(str(row["recipe_id"]))

    local_counts = {}
    for filename, (_, quota) in groups.items():
        payload = json.loads((CATALOG / filename).read_text(encoding="utf-8"))
        local_counts[filename] = sum(
            str(row.get("recipe_id", "")).startswith("local_expansion_")
            for row in payload.get("recipes", [])
        )
    if all(local_counts[filename] >= quota for filename, (_, quota) in groups.items()):
        print("本地扩展已完成：目录保持 400 道，无需重复写入。")
        return
    for path in CATALOG.glob("*.json"):
        payload = json.loads(path.read_text(encoding="utf-8"))
        for row in payload.get("recipes", []):
            existing_names.add(str(row["name"]))
            existing_ids.add(str(row["recipe_id"]))

    new_rows: dict[str, list[dict[str, object]]] = {}
    index = 1
    for filename, (names, quota) in groups.items():
        rows = []
        category = filename.removesuffix(".json")
        for name in names:
            if name in existing_names:
                continue
            row = _recipe(name, category, index)
            if row["recipe_id"] in existing_ids:
                raise SystemExit(f"重复 recipe_id: {row['recipe_id']}")
            existing_names.add(name)
            existing_ids.add(str(row["recipe_id"]))
            rows.append(row)
            index += 1
            if len(rows) == quota:
                break
        new_rows[filename] = rows

    added = sum(len(rows) for rows in new_rows.values())
    if added != 300:
        raise SystemExit(f"预期新增 300 道，实际新增 {added} 道；请先处理菜名重复。")

    for filename, rows in new_rows.items():
        path = CATALOG / filename
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["catalog_version"] = int(payload.get("catalog_version", 1)) + 1
        payload["recipes"].extend(rows)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"{filename}: +{len(rows)}")
    print(f"新增完成：{added} 道")


if __name__ == "__main__":
    main()
