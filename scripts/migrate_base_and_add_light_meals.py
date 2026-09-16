"""Move legacy base recipes into category catalogs and add light meals."""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RECIPES = ROOT / "skills/kitchen_assistant/recipes"
BASE = RECIPES / "recipes.json"

TARGETS = {
    "番茄炒蛋": "vegetables", "土豆丝": "vegetables",
    "可乐鸡翅": "meat", "牛排": "meat", "红烧排骨": "meat",
    "番茄鸡蛋面": "staples_and_soups", "简单汤面": "staples_and_soups",
    "青菜鸡蛋面": "staples_and_soups", "蛋炒饭": "staples_and_soups",
    "咖喱饭": "staples_and_soups", "牛肉面": "staples_and_soups",
}

LIGHT_NAMES = [
    ("鸡胸肉蔬菜沙拉", "鸡胸肉", "生菜、黄瓜、番茄", "低脂高蛋白，清爽饱腹"),
    ("虾仁牛油果沙拉", "虾仁", "牛油果、生菜、玉米粒", "鲜嫩虾仁搭配健康脂肪"),
    ("金枪鱼玉米沙拉", "水浸金枪鱼", "玉米粒、生菜、番茄", "无需开火的快手轻食"),
    ("藜麦鸡肉沙拉", "鸡胸肉", "藜麦、西兰花、胡萝卜", "均衡碳水与蛋白质"),
    ("牛肉蔬菜能量碗", "瘦牛肉", "糙米、西兰花、胡萝卜", "一碗吃够蛋白质和蔬菜"),
    ("西兰花虾仁减脂餐", "虾仁", "西兰花、玉米笋", "少油快炒，清淡鲜美"),
    ("糙米鸡胸肉便当", "鸡胸肉", "糙米、西兰花、鸡蛋", "适合工作日提前准备"),
    ("蒸南瓜鸡胸肉", "鸡胸肉", "南瓜、秋葵", "蒸制少油，口感柔嫩"),
    ("低脂鸡肉卷", "鸡胸肉", "全麦饼、生菜、黄瓜", "方便携带的低脂午餐"),
    ("全麦金枪鱼三明治", "水浸金枪鱼", "全麦面包、生菜、番茄", "十分钟完成的早餐"),
    ("鸡蛋牛油果吐司", "鸡蛋", "全麦吐司、牛油果", "简单健康的早餐组合"),
    ("酸奶水果燕麦杯", "无糖酸奶", "燕麦、蓝莓、香蕉", "无需烹饪的轻盈甜味"),
    ("香蕉花生酱隔夜燕麦", "燕麦", "香蕉、无糖酸奶、花生酱", "提前一晚冷藏即可"),
    ("低脂虾仁蒸蛋", "虾仁", "鸡蛋、菠菜", "嫩滑蒸蛋配鲜虾"),
    ("番茄豆腐汤", "嫩豆腐", "番茄、金针菇", "低热量高水分汤品"),
    ("紫菜虾皮豆腐汤", "嫩豆腐", "紫菜、虾皮", "清淡鲜香，五分钟上桌"),
    ("冬瓜鸡肉丸汤", "鸡胸肉", "冬瓜、香菜", "清爽不油腻的汤餐"),
    ("菌菇鸡胸肉汤", "鸡胸肉", "口蘑、蟹味菇、青菜", "菌香浓郁且热量友好"),
    ("白菜豆腐减脂汤", "嫩豆腐", "大白菜、香菇", "家常食材做出轻盈汤品"),
    ("凉拌鸡丝黄瓜", "鸡胸肉", "黄瓜、香菜", "酸辣开胃，蛋白质充足"),
    ("凉拌木耳洋葱", "黑木耳", "洋葱、香菜", "爽脆低卡的凉菜"),
    ("凉拌西兰花", "西兰花", "蒜、胡萝卜", "简单焯拌，保留蔬菜口感"),
    ("芝麻菠菜鸡蛋沙拉", "菠菜", "鸡蛋、芝麻", "铁元素与蛋白质兼顾"),
    ("彩椒鸡肉沙拉", "鸡胸肉", "彩椒、生菜、玉米粒", "颜色丰富，适合便当"),
    ("苹果鸡肉沙拉", "鸡胸肉", "苹果、生菜、核桃", "果香清甜，口感有层次"),
    ("鹰嘴豆蔬菜沙拉", "鹰嘴豆", "黄瓜、番茄、紫甘蓝", "植物蛋白与膳食纤维丰富"),
    ("毛豆鸡蛋减脂餐", "毛豆", "鸡蛋、玉米粒", "高蛋白小份主食"),
    ("豆腐牛肉生菜包", "瘦牛肉", "豆腐、生菜、彩椒", "用生菜包着吃更清爽"),
    ("三文鱼藜麦碗", "三文鱼", "藜麦、西兰花、黄瓜", "优质脂肪搭配复合碳水"),
    ("鳕鱼蔬菜蒸盘", "鳕鱼", "西兰花、胡萝卜、南瓜", "一盘蒸熟，少油省事"),
    ("鸡胸肉西葫芦面", "鸡胸肉", "西葫芦、番茄", "用西葫芦替代部分面条"),
    ("番茄虾仁荞麦面", "虾仁", "荞麦面、番茄、菠菜", "清爽汤面，饱腹不厚重"),
    ("荞麦鸡丝拌面", "鸡胸肉", "荞麦面、黄瓜、胡萝卜", "适合夏天的低脂主食"),
    ("紫薯鸡蛋早餐碗", "鸡蛋", "紫薯、无糖酸奶、蓝莓", "天然甜味，营养密度高"),
    ("红薯鸡胸肉便当", "鸡胸肉", "红薯、生菜、鸡蛋", "粗粮替代精制主食"),
    ("玉米鸡蛋蔬菜饼", "鸡蛋", "玉米粒、胡萝卜、菠菜", "平底锅少油即可完成"),
    ("豆腐蔬菜蒸饼", "嫩豆腐", "胡萝卜、香菇、鸡蛋", "软嫩易入口，适合晚餐"),
    ("无油煎鸡腿排", "去皮鸡腿肉", "西兰花、番茄", "不额外加油也能煎出香气"),
    ("香煎鳕鱼配芦笋", "鳕鱼", "芦笋、柠檬", "简单调味突出鱼肉鲜味"),
    ("鸡肉紫甘蓝沙拉", "鸡胸肉", "紫甘蓝、黄瓜、玉米粒", "脆爽低脂，适合晚餐"),
]

def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))

def save(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

def main() -> None:
    base = load(BASE)
    legacy = [item for item in base.get("recipes", []) if isinstance(item, dict)]
    by_name = {str(item.get("name")): item for item in legacy}
    for name, category in TARGETS.items():
        if name not in by_name:
            continue
        path = RECIPES / "catalog" / f"{category}.json"
        payload = load(path)
        rows = payload.setdefault("recipes", [])
        if not any(str(row.get("recipe_id")) == str(by_name[name].get("recipe_id")) for row in rows):
            rows.append(by_name[name])
        save(path, payload)
    base["recipes"] = []
    save(BASE, base)

    path = RECIPES / "catalog" / "light_meals.json"
    payload = load(path) if path.exists() else {"catalog_version": 1, "recipes": []}
    existing = {str(row.get("name")) for row in payload.get("recipes", [])}
    for index, (name, protein, vegetables, summary) in enumerate(LIGHT_NAMES, 1):
        if name in existing:
            continue
        veg = [item.strip() for item in vegetables.split("、")]
        ingredients = [{"name": protein, "amount": "120克"}, *[{"name": item, "amount": "适量"} for item in veg], {"name": "橄榄油", "amount": "5毫升"}, {"name": "黑胡椒", "amount": "1克"}, {"name": "食用盐", "amount": "1克"}]
        steps = [{"instruction": f"将{protein}处理干净，切成适口大小。"}, {"instruction": f"将{ '、'.join(veg) }洗净切好。"}, {"instruction": "按菜谱组合食材，少油烹调或直接拌匀即可食用。"}]
        payload["recipes"].append({"recipe_id": f"light_{index:03d}", "name": name, "default_servings": 1, "estimated_time_minutes": 15, "difficulty": "简单", "equipment": ["炒锅"], "summary": summary, "ingredients": ingredients, "steps": steps})
    for recipe in payload["recipes"]:
        for item in recipe.get("ingredients", []):
            if item.get("amount") == "适量" and item.get("name") not in {"黑胡椒", "食用盐"}:
                item["amount"] = "50克"
    save(path, payload)

if __name__ == "__main__":
    main()
