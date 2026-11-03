# 厨房助手架构

2026-11-03 按本次同步的仓库实现核对。代码与共享说明一起发布；真实账号、目标设备及平台体验仍需分别验证。

## 入口与模块职责

```text
文字 / 可选语音转写
  → robot_main.handle_input（明确视觉请求先进入 IngredientVisionService）
  → SkillManager / SkillAgent（活动会话优先，否则本地意图路由）
  → KitchenSession（确定性状态机）
      → RequestParser → RecipeSearchRequest
      → Provider → 本地优先 / AI 完整候选
      → 候选确认 → RecipeNormalizer → 烹饪前检查
      → 步骤 / 本地计时 / 厨房问答
  → response_builder → RuntimeExecutor → MockRobotSDK + VoiceOutput

局域网网页 → KitchenConsoleService → 同一个 KitchenSession
                                → IngredientVisionService
                                → VideoImportService（独立后台任务）
```

`KitchenSession` 保存状态、候选、菜谱、步骤、计时和待确认标记，并提供文字及控制台操作入口。需求收集、搜索展示、菜谱确认、烹饪交互和计时规则分别位于 `recipe_collection.py`、`recipe_discovery.py`、`recipe_confirmation.py`、`cooking_flow.py`、`cooking_timer_flow.py`；`timer_controller.py` 提供计时数据函数。短句意图由 `conversation_intents.py` 统一，食材与忌口词汇由 `ingredient_vocabulary.py` 统一。

Provider、Normalizer 和问答服务通过依赖注入使用。模型提供内容，本地代码决定状态转换和 SDK 调用。`SkillManager` 检查 Manifest、路径、能力及输入输出 Schema，隔离坏 Skill；执行器分别保护五个反馈通道。

## 菜谱、推荐与模型

`QwenConfig` 先读环境变量，再用 `config/qwen.env` 中允许的非空值覆盖。加载器只接受七个千问配置项，不读取根目录 `.env`，不执行配置内容。默认文字/视觉模型分别为 `qwen3-omni-flash`、`qwen3-vl-flash`。配置有效时使用 `QwenAIRecipeProvider`，否则使用本地 `MockRecipeSearchProvider`。

默认先查人工目录和已验证生成缓存，匹配即返回；显式 `bypass_cache` 请求跳过该步骤。按菜名生成一个候选；仅按食材推荐允许 1–3 个完整方案，优先两份，有一份有效结果也返回。多菜组合保留各道菜的完整步骤。常备调料由 `pantry_defaults.py` 统一，显式缺少与忌口优先排除；确认食材覆盖是排序目标，不要求全部强行混成一道菜。

`recipe_contract.py` 同时定义提示词和校验规则。候选逐条校验；后续 JSON 截断时只解码完整对象，不补造半份详情。全部无效且错误可纠正时，Provider 在原时间预算内定向纠错一次；剩余不足 2 秒不纠错，纠错超时最多 8 秒。客户端 `max_retries` 默认 0，与业务纠错、教程补全不同。失败后会话按请求条件尝试本地回退，显式绕过缓存时不回退。

有效详情保存在内存和 `recipes/generated/`，确认候选不会再次请求模型补详情。`MemoryRecipeCache` 另提供可关闭的进程内缓存。`provider_mode` 区分 `mock`、`local_cache` 与 `ai_generated`；AI 不生成虚构网页 URL，对外菜谱元数据隐藏“千问 AI 生成”供应商名称。`OnlineRecipeSearchProvider` 的 `web_search` 仍是占位，不发真实搜索请求。

`dish_profiles.load_catalog()` 聚合固定数据。当前 `recipes/recipes.json` 的菜谱数组为空，保留菜品流程配置；440 道固定菜谱存于 `recipes/catalog/`，其中 90 道参考 HowToCook 提交 `c05758fa661ac4efa0361a987b700a351a22159b`。`recipes/sources.json` 保存来源与许可证；加载时拒绝空 ID、空菜名及重复项，审计脚本检查来源、用量和人数缩放。运行时不联网更新上游。

## 烹饪与计时

菜谱标准化后先复核忌口。肉类及水产通过 `WAITING_MEAT_THAW` 确认新鲜或已完全解冻；鸡蛋等不触发该检查。牛排等菜品可有本地专用流程及确认节点。

计时属于本地会话；暂停冻结剩余秒数，恢复重算截止时间。Normalizer 为加热及腌制、浸泡、静置等等待操作保留或恢复时长，普通洗切、调味、装盘不因此计时。并行准备只选真实、独立的后续步骤，不重复已用调料，不提前热油或空烧；独立锅具只烧清水是既有例外。

- 自然语言推进遇到未启动计时会要求开始计时或明确完成；运行中提前结束需要确认。非导入、非控制台流程的起始步骤，以及特定菜品的确认后续步骤，存在本地自动启动计时分支。
- 控制台选择/导航进入步骤不自动启动计时。主按钮按状态显示食材确认、恢复、开始计时、下一步或完成；导航接口直接切换并清理离开步骤的计时，不再二次确认。未确认食材、暂停、取消和完成状态不能直接导航。
- 普通计时到期提醒检查实际状态；菜品配置的 `timer_end_action=await_confirmation/advance` 可进入确认或推进分支，不能一概描述为到期绝不推进。

`RuleBasedCookingQuestionService` 处理普通问题和事故类能力边界；事故输入暂停指导，不生成具体处置步骤。普通问题未命中本地规则时才调用 Qwen 问答。计时、视觉与模型回答均不能确认食品或现场安全。

## 视觉与局域网控制台

`IngredientVisionService` 同时服务视觉问答和控制台食材盘点。`MacCamera` 用 OpenCV 读取并预热默认相机的少量帧，压缩为内存 JPEG，并在拍摄结束时释放相机。Mac 使用 AVFoundation，其他平台使用默认后端；类名不表示其他平台已完成设备验证。

图片不落盘。上传使用 Data URL，单张限制 8 MB；结果区分可见食材与不确定项。用户增删确认后才提交推荐，不能把识别结果自动当作库存事实。

`KitchenConsoleServer` 使用 `ThreadingHTTPServer` 提供页面和 JSON API。`/api/status` 聚合会话、执行器及相机状态；网页每 500 ms 尝试刷新，复用未完成请求避免轮询重叠。POST/PATCH/DELETE 执行同源校验；它不是用户鉴权，当前仍为单进程、单活动厨房会话。

执行器保留有限反馈事件，用 `feedback_stream_id`、`sequence` 标识事件流与顺序，`execution_id`、`execution_step`、`execution_total` 描述执行阶段。页面展示最新事件并避免重复展示；执行阶段不等于菜谱步骤号。控制台没有火源或高风险设备控制接口。

## 视频与图文教程导入

`VideoImportService` 在独立后台任务中获取和理解教程，不占厨房会话锁执行长请求，也不改变当前烹饪任务。小红书公开分享页按类型进入视频或图文路径；失败时展示原因，视频可上传 MP4/MOV。可选第三方下载通道需要独立配置。

`video_media.py`、`video_network.py` 校验公网地址、重定向、100 MB 大小和 180 秒时长，使用 FFmpeg 检查和转码。120 秒以内且负载允许的视频可一次理解完整菜谱；多段视频按片段理解后用文字证据合并。`recipe_post_media.py` 按原顺序准备图文图片，最多 16 张、单张 8 MB/2400 万像素、准备后总量 16 MB；正文与图片一起整理，纯正文走文字路径。

结果区分教程证据、AI 建议、用户修改和本地调整。剪辑时间不是烹饪时间；缺失信息优先在原次理解中补全，必要时追加受限文字请求。流式预览只读，不能替代完整校验和人工核对。

未确认草稿只在内存中。用户保存编辑、核对标准化结果后，`video_recipe_store.py` 原子写入 `recipes/imported/`。读取时可缩放用量，不重写已确认操作流程。`select_imported_console_recipe` 拒绝覆盖活动任务，再复用忌口、食材状态检查和教学状态机。临时媒体在收尾清理；图像负载和签名图片地址不写入持久化草稿。指标记录阶段时间、首项/首步时间、请求次数及真实返回的 Token 用量，单次样本不是性能保证。

## 语音、交付与验证边界

`voice_io.py` 已实现 VAD 录音、独立 ASR 配置、Edge/pyttsx3 播报，`--voice` 和 `--manual` 复用主入口。`--no-play` 跳过播放。录音写入 `voice_outputs/`，与视觉图片的内存处理不同；真实录音、服务和播放链路仍须设备验证。

`scripts/portable.py` 提供安装、诊断和启动，`scripts/build_portable.py` 使用白名单打包；本地凭据、虚拟环境、媒体、日志和 `doc/` 不在迁移包内。配置、端口和目标电脑操作见 [START_HERE.md](../START_HERE.md)。会话、计时和未确认草稿不跨进程恢复。

Python 测试、Node 页面逻辑测试、真实云端调用与设备/UI 验证分别记录。当前 CI 配置运行 Python 3.11/3.12/3.13 的编译、pytest 和文字冒烟；Node 页面测试由独立 CI 作业运行，也可在本地执行；配置存在不等于测试已经通过。
