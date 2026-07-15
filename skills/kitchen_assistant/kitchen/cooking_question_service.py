from __future__ import annotations

from typing import Protocol

from .models import CookingAnswer, CookingContext


NORMAL = "NORMAL"
CAUTION = "CAUTION"
STOP_AND_CHECK = "STOP_AND_CHECK"


class CookingQuestionService(Protocol):
    def answer(self, question: str, context: CookingContext) -> CookingAnswer | None: ...


class RuleBasedCookingQuestionService:
    """Offline safety-first advice. It never advances or edits a recipe step."""

    def answer(self, question: str, context: CookingContext) -> CookingAnswer | None:
        text = question.replace(" ", "")
        stop = self._stop_answer(text)
        if stop:
            return stop
        if "小锅" in text and "面" in text and any(word in text for word in ("太长", "放不进", "软化", "压进去")):
            return CookingAnswer(
                "这样通常可以：先把面条一端放进沸水，等变软后用筷子或夹子轻轻把剩余部分压入水中，并轻轻搅动防粘。不要让锅外的面条靠近明火；锅太满就减少面量或换大锅。",
                "长面可先软化再压入；远离明火，锅太满就减量。",
            )
        if "面条" in text and any(word in text for word in ("粘", "黏")):
            return CookingAnswer("面条下锅后前一两分钟轻轻搅动；水量太少可补一点开水，避免一次放太多面。", "前两分钟轻搅，必要时补开水。")
        if any(word in text for word in ("烧开", "开了吗", "沸腾")):
            return CookingAnswer("水面持续冒出较大的气泡、气泡翻滚且蒸汽明显时，通常就是烧开了。", "持续翻滚大气泡 = 水已烧开。")
        if "水放多" in text:
            return CookingAnswer("水多一点通常没关系；可以多煮一会儿让汤收浓，或先盛出少量热汤再调味。", "水多可稍收汤，或先盛出少量热汤。")
        if "水放少" in text or "水不够" in text:
            return CookingAnswer("请沿锅边少量补加开水，不要一次加太多冷水，以免温度骤降。", "少量补开水，避免一次加冷水。", CAUTION, False, "nod", "yellow", "focused")
        if "白糖" in text and any(word in text for word in ("没有", "不放", "替代")):
            return CookingAnswer("没有白糖可以不放；番茄类菜可多炒一会儿让自然甜味释放，口味会稍微酸一些。", "白糖可省略，番茄多炒一会儿。")
        if "葱" in text and any(word in text for word in ("没有", "不放", "不要")):
            return CookingAnswer("可以不放葱，不影响基本做法；最后按口味补一点盐即可。", "葱可省略。")
        if "不吃辣" in text or "不要辣" in text:
            return CookingAnswer("可以不放辣椒和辣酱，其他步骤照常进行。", "不放辣椒和辣酱即可。")
        if "锅太小" in text:
            return CookingAnswer("锅太小时请减少一次下锅的量，给水和食材留出翻滚空间；容易溢锅的食材可分两次做。", "锅太小：减量或分两次做，留出空间。", CAUTION, False, "nod", "yellow", "focused")
        if "高压锅" in text and any(word in text for word in ("没有", "不用", "替代", "怎么办")):
            return CookingAnswer(
                "没有高压锅可以改用普通带盖汤锅：加入足量热水没过肉类，先烧开后转小火慢炖，通常需要60到90分钟。每15到20分钟查看一次水量，必要时补开水；用筷子能较容易插入时再进入收汁或下一步。",
                "无高压锅：普通锅小火慢炖60–90分钟，每15–20分钟查水量。",
                CAUTION, False, "nod", "yellow", "focused",
            )
        if any(word in text for word in ("有点糊", "一点糊", "快糊", "粘锅糊")):
            return CookingAnswer(
                "先把火调小或暂时关火，把还没焦的部分轻轻移到干净区域或盛出，不要刮起锅底焦黑部分；确认没有大量烟或起火后再决定是否继续。",
                "轻微焦糊：先降火，移出未焦部分，不刮锅底。",
                CAUTION, False, "stop", "yellow", "warning",
            )
        if any(word in text for word in ("什么火", "火候", "大火还是小火")):
            heat = context.current_step.get("heat_level") or "中火"
            return CookingAnswer(f"当前这一步建议用{heat}。如果锅里水分快烧干或油温过高，就及时调小火。", f"当前建议：{heat}", CAUTION, False, "nod", "yellow", "focused")
        if "忘记" in text and "调料" in text:
            return CookingAnswer("还没关火的话可以先少量补加调料并尝味；不要一次加太多，尤其是盐。", "少量补调味，边尝边加。")
        return None

    @staticmethod
    def _stop_answer(text: str) -> CookingAnswer | None:
        if "起火" in text or "着火" in text:
            return CookingAnswer("先停止加热；不要向油火泼水。若火势无法立即安全控制，请远离现场并联系当地紧急服务。", "请先停止加热并检查安全：油火不要泼水。", STOP_AND_CHECK, True, "stop", "red", "warning")
        if any(word in text for word in ("燃气味", "煤气味", "闻到燃气", "闻到煤气")):
            return CookingAnswer("先关闭火源；不要开关电器或使用明火，开窗通风并离开有气味区域，必要时联系燃气服务。", "疑似燃气泄漏：关火、勿动电器、通风并远离。", STOP_AND_CHECK, True, "stop", "red", "warning")
        if "大量" in text and "烟" in text:
            return CookingAnswer("先关闭加热并保持距离，确认锅内没有起火；烟持续或刺激明显时请通风并寻求现场帮助。", "大量冒烟：先停火，保持距离并检查。", STOP_AND_CHECK, True, "stop", "red", "warning")
        if "烫伤" in text or "烫到" in text:
            return CookingAnswer("请先停止当前操作并远离热源；用流动凉水持续冷却烫伤处，伤势严重或不确定时及时寻求医疗帮助。", "疑似烫伤：先停下并用流动凉水冷却。", STOP_AND_CHECK, True, "stop", "red", "warning")
        if "电器进水" in text or ("电器" in text and "进水" in text):
            return CookingAnswer("请先停止使用该电器；不要徒手接触可能带电的部位，必要时在确保安全的前提下切断电源并联系专业人员。", "电器进水：停止使用，勿触碰带电部位。", STOP_AND_CHECK, True, "stop", "red", "warning")
        if "油溅" in text or ("油" in text and "溅" in text) or "油一直在响" in text or "油温过高" in text:
            return CookingAnswer("油温可能偏高，请先调小火或暂时离火，食材擦干后再下锅，并保持手脸远离锅沿。", "油温偏高：调小火，食材擦干，远离锅沿。", CAUTION, False, "stop", "yellow", "warning")
        return None


class LLMCookingQuestionService:
    """Reserved fallback wrapper until an approved LLM contract is supplied.

    This class deliberately delegates to the local safety/rule service. A
    future structured LLM result must be validated before becoming a
    ``CookingAnswer`` and may never change the session's current step.
    """

    def __init__(self, fallback: CookingQuestionService | None = None) -> None:
        self.fallback = fallback or RuleBasedCookingQuestionService()

    def answer(self, question: str, context: CookingContext) -> CookingAnswer | None:
        return self.fallback.answer(question, context)


class DoubaoCookingQuestionService:
    """Rules first; Doubao only answers ordinary questions that rules miss."""

    def __init__(self, llm_client: object, fallback: CookingQuestionService | None = None) -> None:
        self.llm_client = llm_client
        self.fallback = fallback or RuleBasedCookingQuestionService()

    def answer(self, question: str, context: CookingContext) -> CookingAnswer | None:
        local = self.fallback.answer(question, context)
        if local is not None:
            return local
        if not getattr(self.llm_client, "is_available")():
            return None
        try:
            from llm.prompts import cooking_question_messages

            content = self.llm_client.chat(cooking_question_messages(question, {
                "recipe": context.recipe.get("name"),
                "current_step": context.current_step,
                "servings": context.servings,
                "taste_preferences": context.taste_preferences,
                "dietary_restrictions": context.dietary_restrictions,
                "available_ingredients": context.available_ingredients,
                "available_equipment": context.available_equipment,
                "timer_remaining_seconds": context.timer_remaining_seconds,
            }))
            answer = str(content).strip()
            if not answer or len(answer) > 800:
                raise ValueError("回复为空或过长")
            return CookingAnswer(answer, answer[:80], NORMAL, False, "nod", "blue", "focused")
        except Exception:
            return CookingAnswer("这个问题我暂时无法可靠判断。请先保持当前步骤不变，确认火候和安全后再继续。", "无法可靠判断：请先保持当前步骤并检查安全。", CAUTION, False, "nod", "yellow", "focused")
