# 厨房助手架构（公开版本）

2026-11-03 按 `main` 的源码快照 `e645b53` 核对。本文描述可下载的公开代码。局域网控制台、教程导入、440 道固定菜谱和便携启动脚本已在本地继续开发，尚未包含在本次公开源码中，见 [版本范围](../README.md#published-version-and-local-development)。

## 运行链路

```text
文字 / 可选语音转写
  → robot_main.handle_input
  → 明确视觉请求：IngredientVisionService → 相机 → Qwen VL
  → 其他输入：SkillManager / SkillAgent（活动会话优先）
      → KitchenSession → 请求收集 → Provider → 候选确认
      → RecipeNormalizer → 烹饪前检查 → 步骤 / 本地计时 / 问答
  → response_builder → RuntimeExecutor → MockRobotSDK + VoiceOutput
```

`KitchenSession` 保存状态、候选、菜谱、步骤和计时。需求收集、搜索、确认、烹饪和计时分别位于 `recipe_collection.py`、`recipe_discovery.py`、`recipe_confirmation.py`、`cooking_flow.py`、`cooking_timer_flow.py`。`conversation_intents.py` 统一短句，`ingredient_vocabulary.py` 统一食材与忌口词汇。模型不能直接改变状态或调用硬件。

`SkillManager` 检查 Manifest、能力及输入输出 Schema，隔离坏 Skill。执行器逐通道处理语音、屏幕、动作、灯带和表情；异常或未知枚举分别降级。动作等使用 Mock，语音由可选 `VoiceOutput` 后端播放或仅打印。

## 菜谱与模型

公开版本的 `llm/config.py` 只读取环境变量，不自动加载 `.env` 或 `config/qwen.env`。文字/视觉默认模型分别为 `qwen3-omni-flash`、`qwen3-vl-flash`，关闭深度思考。配置 AI 后使用 `QwenAIRecipeProvider`，默认仍先查本地目录和已验证生成缓存；命中即返回，不为替换本地结果调用模型。

本地未命中时，一次响应生成一个完整候选及其 6–10 步 RawRecipe，经过 JSON、语义和标准化校验。当前公开版 `candidate_limit()` 固定为 1，不能套用本地新版的 1–3 个方案策略。`recipe_contract.py` 同时提供提示词约束与校验规则；有效详情保存在内存和生成缓存，确认时直接复用。

非法 JSON 或可纠正的校验失败可触发一次定向纠错，计入原时间预算；剩余不足 2 秒不纠错，纠错超时最多 8 秒且不超过剩余预算。SDK `max_retries` 默认 0，与这次业务纠错不同。仍失败时会话尝试本地回退。公开版尚无新版的截断响应完整候选恢复机制。

`provider_mode` 区分 `mock`、`local_cache`、`ai_generated`。AI 内容没有虚构网页 URL。`MemoryRecipeCache` 是可关闭的进程内缓存；`OnlineRecipeSearchProvider` 的 `web_search` 仍是占位，不发真实搜索请求。

固定目录共 100 道：`recipes/recipes.json` 中 10 道基础菜谱及流程配置，加上 `recipes/catalog/` 中 90 道参考 HowToCook 固定提交 `c05758fa661ac4efa0361a987b700a351a22159b` 的菜谱。来源和许可证保存在 `recipes/sources.json`，加载拒绝重复 ID/菜名。目录审计检查来源、用量与人数缩放，运行时不更新上游。生成缓存是另一个数据来源，不计入 100 道固定目录。

## 烹饪与计时

确认菜谱后复核忌口；肉类和水产要求确认新鲜或完全解冻。牛排等流程由本地菜品规则控制，包括确认节点和后续计时。

计时属于会话，暂停冻结剩余时间，恢复重算截止时间。Normalizer 为加热及腌制、浸泡、静置等等待保留或恢复时长；普通洗切、调味、装盘不自动计时。常规步骤通过用户计时指令启动；特定确认节点完成后可由本地流程启动下一段计时。

自然语言提前推进未启动的计时步骤时，要求开始计时或明确完成；运行中提前结束要求确认。普通到期提醒检查实际状态，特定菜品的 `timer_end_action` 可进入确认或推进分支。并行准备只使用真实、独立的后续步骤，避免重复调料和提前热油等操作。

厨房问答先走本地规则，风险相关输入可触发 `STOP_AND_CHECK`、停止动作和暂停状态，普通问题未命中时再调用 Qwen。这里描述代码分流，不表示规则已经过事故处置或食品安全验证。模型和计时不能确认现场安全或熟度。

## 视觉、语音与验证边界

视觉请求在 Skill 路由前识别。`MacCamera` 用 OpenCV 拍摄单帧并释放相机，图片在内存中传给 Qwen VL，校验后返回五通道反馈。结果只回答本轮视觉问题，不自动修改厨房会话，也不判断过敏原、成熟度或食品安全。公开版没有照片库存确认页面。

`--voice` 和 `--manual` 已有录音、VAD、ASR、TTS 代码。ASR 使用独立的 MIMO 环境配置，录音写入 `voice_outputs/`；真实设备及连续对话仍需验证。文字模式有后台轮询计时事件；语音模式的录音/播报期间提醒体验需要单独检查。

公开版本是单进程、单活动会话，不跨重启恢复。CI 配置覆盖 Python 3.11/3.12/3.13 的编译、pytest 和文字冒烟；本次只更新文档，没有重新运行测试、云端或硬件验证。
