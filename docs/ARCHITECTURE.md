# 厨房助手架构

```text
用户输入
  -> SkillAgent / SkillManager（活动厨房会话优先）
  -> KitchenSession（确定性状态机）
     -> RequestParser -> RecipeSearchRequest
     -> RecipeSearchProvider -> 本地优先；未命中时生成 1 个带完整详情的候选
     -> 单候选直接确认
     -> RecipeNormalizer -> 内部 Recipe
     -> CookingQuestionService（普通烹饪问答；事故处置硬边界）
  -> response_builder（五类反馈）
  -> RuntimeExecutor -> MockRobotSDK
```

`KitchenSession` 只保留状态、候选、已选菜谱、步骤、计时器和依赖，并负责分发。需求收集与搜索展示由 `recipe_collection.py`、`recipe_discovery.py` 负责；菜谱确认与烹饪前检查在 `recipe_confirmation.py`；烹饪步骤和问答在 `cooking_flow.py`；计时状态与并行准备交互在 `cooking_timer_flow.py`；纯计时数据函数仍在 `timer_controller.py`。`conversation_intents.py` 统一“好/开始/换一个”等会话意图，`ingredient_vocabulary.py` 统一食材同义词、类别、忌口类别、调味料和动物蛋白判断。Provider、Normalizer、问答服务通过依赖注入使用，LLM/Agent 不能改变步骤或直接调用硬件。含生鲜肉类、鱼虾蟹贝的菜谱开始前会确认其为新鲜食材或已完全解冻；鸡蛋等无需解冻确认。牛排还会收集熟度，并由本地状态机控制“开始煎 → 正面计时 → 翻面确认 → 另一面计时 → 静置”。

当 `DASHSCOPE_API_KEY` 存在时，`QwenAIRecipeProvider` 仍然先调用本地 Provider，统一查询人工目录和已验证生成缓存。只要本地存在符合菜名、忌口、厨具和库存约束的结果，就立即返回，不发送任何云端请求，也不显示“生成服务不可用”的降级提示。本地完全未命中时，`QwenLLMClient` 才使用 Qwen Omni 的流式 Chat Completions；一次响应只返回 1 个候选及其完整 6–10 步 RawRecipe，用户可直接用“好”确认。`recipe_contract.py` 同时提供 Prompt 硬约束和结构校验常量；输出经过 JSON 解析、语义校验与 `RecipeNormalizer`。所有模型请求关闭深度思考且不自动重试；非法 JSON 直接降级，不发起修复请求。业务元数据会标注 `provider_mode=ai_generated`、`local_cache` 或 `mock`。AI 来源仍在内部对象中保留用于诊断，但会话对外 JSON 会隐藏供应商 `source_name`，URL 保持 `null`。

候选排序由 `recommendation_service.py` 完成：忌口冲突先过滤，然后综合指定菜名、已有食材命中与缺失、时间、简单偏好、辣/少盐偏好及厨具匹配排序。`MemoryRecipeCache` 是可关闭的进程内缓存，不存储密钥或无效结果。

`OnlineRecipeSearchProvider` 是 `web_search` 占位，当前不发 HTTP 请求，也不会抓取随机网页。后续搜索能力应继续在这个边界内实现。`RuleBasedCookingQuestionService` 处理常见做菜问题；起火、燃气、触电或受伤等事故类输入统一返回能力边界声明、暂停指导，不生成具体处置步骤。仅在本地规则未命中普通问题时才由 `QwenCookingQuestionService` 调用千问，且模型提示同样禁止事故判断与处置建议。

视觉识别在 `robot_main.handle_input` 的 Skill 路由之前执行，只拦截包含明确指示词的食材视觉请求。`MacCamera` 通过 OpenCV 从默认摄像头读取并预热少量帧，在内存中压缩为约 720p JPEG；`IngredientVisionService` 同时服务对话识别和控制台食材盘点，维护摄像头读取状态，并校验 Qwen VL 的精简 JSON。原始图片不落盘、不写日志，请求结束立即释放摄像头。控制台盘点结果必须由用户确认后才写入厨房会话，不用于确认成熟度、过敏原或食品安全。

`KitchenConsoleServer` 使用标准库 `ThreadingHTTPServer` 提供静态页面和小型 JSON API。`/api/status` 聚合 `KitchenSession.status_snapshot()`、`RuntimeExecutor.status_snapshot()` 与摄像头状态；图片上传使用 JSON Data URL，限制 8 MB；所有 POST 执行同源校验。`recommend_from_ingredients` 是厨房 Skill 的受信任主机 Hook，它会使用确认食材重置并进入候选展示状态。控制台没有高风险设备控制接口。

视频导入由独立的 `VideoImportService` 后台任务处理，不在厨房会话锁内执行下载或模型请求。平台适配器读取公开小红书移动分享页，或使用显式配置的第三方 API；获取失败后用户可上传 MP4/MOV。媒体层检查公开地址与重定向、100 MB 大小及 180 秒时长，使用 FFmpeg 校验和转码。模型先提取视频事实，再整理教学草稿；草稿保留原视频事实、AI 建议、用户修改和本地标准化调整的来源标记。

用户确认前，草稿独立于活动厨房会话；处理收尾阶段清理临时媒体。确认后的菜谱原子写入独立的 `recipes/imported/` 目录，读取时仅缩放用量，不重复调整已确认的操作流程。`select_imported_console_recipe` 在会话锁内拒绝覆盖活动任务，并复用菜谱确认后的忌口检查、动物蛋白状态确认和教学状态机。导入菜谱的步骤计时由用户明确启动，控制台导航也遵守未启动计时与提前结束的确认流程。新增 POST/PATCH/DELETE 接口均执行同源校验，multipart 视频上传有独立的大小上限。

活动计时器属于本地会话状态。进入 `PAUSED` 时冻结剩余秒数，恢复后重新计算截止时间；AI 不能启动、暂停或修改计时。Normalizer 只保留煮、炖、焖、煎、烤、蒸、炸、焯、收汁等加热步骤的 `duration_seconds`，准备和装盘步骤不会触发计时。菜谱标准化后、开始烹饪前还会执行本地忌口复核，避免 AI 详情或本地回退与用户过敏/忌口冲突；肉类解冻由 `WAITING_MEAT_THAW` 统一处理，模型生成的重复解冻步骤会被移除。

`robot_main.py` 的文字模式会轮询 `SkillManager.poll_active_skill()`；厨房 Skill 仅在本地计时到期时返回一条新反馈，RuntimeExecutor 再照常执行五项 Mock 调用。执行器只读取反馈字段并独立保护五项 Mock 调用，同时把不在老师提供的 Mock SDK 枚举内的动作/灯带值安全降级。业务元数据（候选、来源、当前菜谱、状态）保留在顶层，执行器不会将它们交给硬件层。

本地固定菜谱由 `dish_profiles.load_catalog()` 聚合：`recipes/recipes.json` 保存基础菜谱和菜品流程配置，`recipes/catalog/**/*.json` 按稳定文件名顺序追加分类菜谱。加载阶段统一拒绝空 ID、空菜名、跨文件重复 ID 和重复菜名。`recipes/sources.json` 单独保存上游版本与许可证，不混入会话状态。当前人工校订数据固定到 HowToCook 提交 `c05758fa661ac4efa0361a987b700a351a22159b`，运行时不联网；`scripts/validate_recipe_catalog.py` 对来源一致性、具体用量及所有支持人数执行离线审计。
