from __future__ import annotations

import random
from collections.abc import Sequence


# Conversation copy belongs here rather than in session-state code or recipe
# JSON. Recipe files contain ingredients and cooking instructions only.
SINGLE_DINER_COMPANIONS = (
    "一个人吃饭也值得好好照顾自己，我陪你慢慢做。",
    "一个人吃饭，也要认真对待这一餐，我会一直陪着你。",
    "一个人吃饭并不孤单，今天这顿我们一起慢慢完成。",
    "一个人吃饭也可以很有仪式感，我来陪你把它做好。",
    "一个人吃饭，更要记得好好照顾自己，我们一步一步来。",
    "一个人吃饭也能很温暖，接下来的步骤交给我陪你。",
)

GRATITUDE_RESPONSES = (
    "不客气，我会继续陪你慢慢做。",
    "不用谢呀，有我在，按自己的节奏来就好。",
    "不客气，随时问我，咱们接着做。",
    "不用谢呀，能陪你完成这顿饭我也很开心。",
    "不客气，今天辛苦的是大厨你自己呀。",
    "不用谢呀，下次想做什么也可以继续叫我。",
)

WAITING_ACKNOWLEDGMENTS = (
    "好的，我在这儿陪着你，完成这一步后请告诉我哦。",
    "慢慢来，做好后我们再继续下一步。",
    "你按当前步骤慢慢来，有问题随时问我哦，做好了请告诉我。",
    "好～我先在这里等你，完成后叫我一声。",
    "好呀，不用着急，按自己的节奏来，做好了请告诉我哦。",
    "注意安全哦，做好这一段我们再往下走，做完了记得告诉我哦。",
)

STEP_ENCOURAGEMENTS = (
    "做得很稳！", "很好，节奏很好。",
    "太棒了，这一步完成得很漂亮。",
    "继续保持，你越来越熟练了。",
    "很不错。",
    "你太厉害了！",
    "动作越来越熟练了。",
    "这一步完成得非常顺利！",
    "干得漂亮！",
    "不错不错，我们继续。",
    "看起来很成功！",
    "比想象中顺利呢。",
    "很好，没有问题！",
    "越来越像大厨了！",
    "非常棒，继续下一步。",
    "这一步拿捏得不错！",
    "节奏掌握得很好。",
    "继续，我们马上就做好了。",
    "很稳，保持这个节奏。",
    "真不错，我感觉已经闻到香味了。",
    "你做饭挺有天赋的。",
    "一步一步来，完全没问题。",
    "看得出来你很认真。",
    "继续保持，我们离成功越来越近了！",
    "太好了，我们继续下一步。",
    "这一步完成得很到位。",
    "不错，继续交给我带着你做。",
)

# These are the user's original completion lines. Keep all of them and format
# only the dish placeholder at runtime.
FINISHED_RESPONSES = (
    "怎么这么香！{dish}完成啦！",
    "哇！好香啊！{dish}大功告成！",
    "简直色香味俱全！{dish}就做好啦！",
    "真棒真棒，{dish}出锅啦，你太厉害了！",
    "恭喜！{dish}已经完成，可以开饭啦！",
    "闻起来真的好香！{dish}顺利完成！",
    "这卖相已经可以发朋友圈了！{dish}做好啦！",
    "恭喜解锁一道新菜：{dish}！",
    "成功啦！{dish}已经可以上桌了！",
    "不错不错，{dish}已经完成，赶紧趁热吃吧！",
    "今天的大厨就是你！{dish}完成啦！",
    "太有成就感了！{dish}新鲜出锅！",
    "这一锅真的很成功！{dish}完成啦！",
    "香味已经飘出来了！{dish}做好了！",
    "完美收工！{dish}可以享用啦！",
    "第一次也能做得这么好，{dish}完成！",
    "太棒了，我们一起完成了这道{dish}！",
    "今天这顿饭必须给自己点个赞！{dish}完成啦！",
    "有没有闻到幸福的味道？{dish}已经做好啦！",
    "好耶！{dish}正式完成，开饭时间到！",
)


class RandomPhrasePicker:
    """Randomly choose copy while avoiding an immediate repeat per category."""

    def __init__(self, rng: random.Random | random.SystemRandom | None = None) -> None:
        self._rng = rng or random.SystemRandom()
        self._last: dict[str, str] = {}

    def choose(self, category: str, phrases: Sequence[str]) -> str:
        if not phrases:
            raise ValueError("文案列表不能为空")
        previous = self._last.get(category)
        choices = [phrase for phrase in phrases if phrase != previous] or list(phrases)
        selected = self._rng.choice(choices)
        self._last[category] = selected
        return selected
