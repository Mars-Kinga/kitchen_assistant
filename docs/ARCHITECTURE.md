# 厨房助手架构

```text
用户输入
  -> SkillAgent / SkillManager（活动厨房会话优先）
  -> KitchenSession（确定性状态机）
     -> RequestParser -> RecipeSearchRequest
     -> RecipeSearchProvider -> RecipeCandidate[最多 3]
     -> 用户选择 + 明确确认
     -> RecipeNormalizer -> 内部 Recipe
     -> CookingQuestionService（安全规则优先）
  -> response_builder（五类反馈）
  -> RuntimeExecutor -> MockRobotSDK
```

`KitchenSession` 保留状态、候选、已选菜谱、步骤、偏好、肉类解冻确认和计时器；Provider、Normalizer、问答服务通过依赖注入使用。Provider 只取得菜谱，Normalizer 只统一数据，LLM/Agent 不能改变步骤或直接调用硬件。含肉类的结构化菜谱在进入烹饪前先进入 `WAITING_MEAT_THAW`；牛排还会收集熟度，并由本地状态机控制“开始煎 → 正面计时 → 翻面确认 → 另一面计时 → 静置”。

当 `ARK_API_KEY` 存在时，`DoubaoLLMClient` 通过普通 Chat Completions 调用 `DoubaoAIRecipeProvider`：先为请求生成 1 至 3 个候选，再为已选候选生成 RawRecipe。输出须经过 JSON 解析、一次有限修复、结构校验与 `RecipeNormalizer`；失败或未配置 Key 时切换到 `MockRecipeSearchProvider`。业务元数据会标注 `provider_mode=ai_generated` 或 `provider_mode=mock`。AI 来源仍在内部对象中保留用于诊断，但会话对外 JSON 会隐藏供应商 `source_name`，URL 保持 `null`。

候选排序由 `recommendation_service.py` 完成：忌口冲突先过滤，然后综合指定菜名、已有食材命中与缺失、时间、简单偏好、辣/少盐偏好及厨具匹配排序。`MemoryRecipeCache` 是可关闭的进程内缓存，不存储密钥或无效结果。

`OnlineRecipeSearchProvider` 是 `web_search` 占位，当前不发 HTTP 请求，也不会抓取随机网页。后续推荐在这个边界内接入火山方舟 Responses API 的 Web Search 工具：搜索层保存查询、摘要、引用 URL 和获取时间，再交给现有 Normalizer；状态机、候选确认、安全校验和硬件反馈均不改变。接入前必须确认账号组件、模型兼容性和真实响应结构。`RuleBasedCookingQuestionService` 先处理起火、燃气味、大量浓烟、烫伤和电器进水等安全情况，再处理常见新手问题；仅在它们未命中时才由 `DoubaoCookingQuestionService` 调用豆包。豆包失败时返回本地安全 fallback，且不会改变步骤或状态。

活动计时器属于本地会话状态。进入 `PAUSED` 时冻结剩余秒数，恢复后重新计算截止时间；AI 不能启动、暂停或修改计时。Normalizer 只保留煮、炖、焖、煎、烤、蒸、炸、焯、收汁等加热步骤的 `duration_seconds`，准备和装盘步骤不会触发计时。菜谱标准化后、开始烹饪前还会执行本地忌口复核，避免 AI 详情或本地回退与用户过敏/忌口冲突；肉类解冻由 `WAITING_MEAT_THAW` 统一处理，模型生成的重复解冻步骤会被移除。

`robot_main.py` 的文字模式会轮询 `SkillManager.poll_active_skill()`；厨房 Skill 仅在本地计时到期时返回一条新反馈，RuntimeExecutor 再照常执行五项 Mock 调用。执行器只读取反馈字段并独立保护五项 Mock 调用，同时把不在老师提供的 Mock SDK 枚举内的动作/灯带值安全降级。业务元数据（候选、来源、当前菜谱、状态）保留在顶层，执行器不会将它们交给硬件层。
