# AI 厨房助手 Skill

## 功能

`kitchen_assistant` 是单进程、多轮对话的厨房指导 Skill。它负责理解做菜需求、推荐候选、确认菜谱、标准化用量和步骤、逐步指导、本地计时、安全问答与完成反馈。

目前没有直接控制机器人硬件；返回结构化结果，由 Runtime 的 `RuntimeExecutor` 调用语音、屏幕、动作、灯带和表情能力。

## 触发方式

触发词定义在 `skill.json`，包括“厨房助手”“我想做”“我想吃”“我要吃”“菜谱”“不知道做什么”以及常见菜名和食材。`runtime_core.agent.SkillAgent` 还会识别包含食物词的烹饪问法和库存表达。

示例：

```text
我想做番茄炒蛋
教我做红烧排骨
我只有蘑菇和牛肉，不知道做什么
厨房助手，我有胡萝卜和鸡肉，能做什么菜
```

活动会话建立后，“1人”“正常”“第二个”“开始”“下一步”等短句会继续路由到本 Skill，直到用户输入 `再见`。

## 输入

入口：`scripts/run.py:run(arguments)`。

输入 Schema：`schemas/input_schema.json`。

```json
{
  "user_text": "用户本轮原始文本"
}
```

`user_text` 必须是非空字符串，不允许额外字段。人数、口味、食材、忌口、厨具、时间和难度均从多轮自然语言中解析并保存在进程内会话。

## 输出

输出 Schema：`schemas/output_schema.json`。

顶层固定字段：

- `route="skill_result"`
- `task_name="AI 厨房助手"`
- `kitchen_state`
- `session_active`
- `provider_mode`

单条反馈必须有 `speech` 或 `question`，并同时包含：

- `display`
- `robot_action`
- `led_effect`
- `expression`

一次返回多个连续反馈时使用 `steps` 数组，数组中每项都遵守同一五通道契约。候选、当前菜谱、当前步骤、安全级别等通过可选元数据返回。

Runtime 会在调用前校验输入，在调用后及异步 `poll()` 返回时校验输出。违约结果不会进入执行器，而会转成安全错误反馈。

## 状态机

```text
IDLE
  ├─ COLLECTING_REQUEST
  ├─ COLLECTING_INGREDIENTS
  └─ COLLECTING_PREFERENCES
       → SEARCHING_RECIPES
       → PRESENTING_CANDIDATES
       → WAITING_RECIPE_CONFIRMATION
       → WAITING_MEAT_THAW（需要时）
       → COOKING ↔ PAUSED
       → COMPLETED / CANCELLED
```

`kitchen/session_store.py` 只保存会话数据并分发状态；需求收集、搜索展示、菜谱确认、烹饪步骤、计时交互分别位于 `recipe_collection.py`、`recipe_discovery.py`、`recipe_confirmation.py`、`cooking_flow.py`、`cooking_timer_flow.py`。`conversation_intents.py` 统一短确认词，`ingredient_vocabulary.py` 统一食材和忌口词汇。Provider 或 LLM 不能直接推进状态、结束计时或调用机器人能力。

## 菜谱来源

- `mock`：读取 `recipes/recipes.json` 和按文件名排序的 `recipes/catalog/**/*.json`；目录加载时拒绝重复 `recipe_id` 或菜名。
- `ai_generated`：配置 `DASHSCOPE_API_KEY` 后由千问 Chat Completions 生成。
- `local_cache`：读取 `recipes/generated/` 中已通过校验的生成菜谱。
- `web_search`：预留模式，当前未接通真实搜索。

本地固定目录当前恰好包含 100 道菜，其中 `recipes/catalog/` 有 90 道参考 HowToCook 固定提交 `c05758fa661ac4efa0361a987b700a351a22159b` 的校订菜谱。每道菜都记录 `source_key`、`source_revision`、固定链接和许可证；`recipes/sources.json` 统一记录上游仓库、Unlicense 和本项目做过的安全/用量转换。运行时不访问上游。目录变更后运行 `python scripts/validate_recipe_catalog.py`，检查总数、来源、重复项、具体用量和 1/2/3 人份标准化。`python scripts/prune_generated_recipe_duplicates.py --apply` 可删除核心菜名已被固定目录覆盖的生成缓存。

Provider 始终先查询本地固定目录和已验证生成缓存；只要存在符合菜名、忌口、厨具和库存约束的本地结果，就直接使用且不调用千问。本地完全没有匹配结果时，AI 在一次响应中生成一个候选及其完整 6–10 步详情。单候选可直接确认。完整菜谱保存为按菜名命名的缓存文件，后续优先复用；人数变化由 `RecipeNormalizer` 缩放食材和步骤用量。

AI 生成不是网页搜索，不提供虚构 URL。Provider 调用失败时可回退本地 Mock；缺少有效完整详情的 AI 候选不会显示，也不会在用户确认后再次调用模型。

## 计时规则

`RecipeNormalizer` 只为确实依赖时间的操作保留或恢复 `duration_seconds`：

- 火候：预热、加热、煮、炖、焖、煎、烤、蒸、炸、焯、炒、收汁等。
- 等待：腌制、浸泡、泡发、静置、醒发等。

洗切、搅拌、普通调味和装盘不自动计时。

用户说“开始”“下锅了”“开始计时”等才启动当前步骤计时。带时长步骤尚未启动时，“下一步”“跳过”或明确完成语句不会直接推进；助手要求用户选择：

- “开始计时”：由本地计时器执行；
- “确认完成”：用户明确表示已自行计时并检查完成状态。

运行中的计时若要提前结束，也需要二次确认。计时到期只提示检查实际状态，不自动宣称已熟或自动推进。

## 腌制/浸泡期间的并行规则

只有后续步骤同时满足以下条件时才提供并行建议：

1. 它是菜谱中真实存在的后续步骤；
2. 可独立完成，不接触正在等待的肉类或食材；
3. 不重复当前步骤已经量取或加入的食材、调料；
4. 出于安全考虑，不提前热油、空烧锅、炒糖色、油炸或开始后续正式烹饪；
5. 例外仅允许在独立锅具中只烧清水这样的安全操作。

用户确认并行准备完成后会记录其步骤索引；当前计时不会被打断，后续到达该步骤时再次确认后跳过。

没有合规候选时不启动并行流程，也不虚构“准备料酒/调料碗”等工作，统一提示：

```text
在这段时间里你可以同步做自己想做的事情，时间到了我会叫你～
```

`llm/prompts.py` 对 AI 菜谱提出同样约束，本地 `_parallel_prep_candidate()` 负责最终兜底。

## 食品安全与异常处理

- 含生鲜肉类、鱼虾蟹贝的菜谱开始前进入 `WAITING_MEAT_THAW`，用户可确认它是新鲜食材或已经完全解冻；鸡蛋和奶制品不进入该状态。
- 起火、燃气味、大量浓烟、烫伤和电器进水等风险优先由本地规则处理，并可暂停流程。
- 肉类计时只是参考下限，必须检查完全变色、中心无粉红或使用合适温度计。
- 助手不声称看见现场，不保证食物已熟。
- 无候选、Provider 超时、AI JSON 错误、缓存写入失败和问答服务不可用均有本地降级路径。
- Skill 异常或输出不符合 Schema 时，`SkillManager` 会结束活动会话并返回五通道安全错误，不影响其他 Skill。

## 依赖

运行依赖由项目根目录 `requirements.txt` 管理，测试依赖由 `requirements-dev.txt` 管理。主要外部包包括：

- `openai`：千问 OpenAI 兼容 Chat Completions；
- `opencv-python`：Mac 摄像头单帧拍摄与 JPEG 压缩；
- `jsonschema`：Manifest 引用的输入/输出契约校验；
- `numpy`、`scipy`、`sounddevice`：语音采集链路；
- `edge-tts`、`pyttsx3`、`pygame`：语音播放链路。

厨房文字流程和默认测试不需要网络或 Key。

## 配置

千问：

- `DASHSCOPE_API_KEY`
- `QWEN_BASE_URL`
- `QWEN_TEXT_MODEL`（默认 `qwen3-omni-flash`）
- `QWEN_VISION_MODEL`（默认 `qwen3-vl-flash`）
- `QWEN_TIMEOUT_SECONDS`（默认 `25` 秒）
- `QWEN_VISION_TIMEOUT_SECONDS`（默认 `20` 秒）
- `QWEN_MAX_RETRIES`（默认 `0` 次）

Key 只从环境变量读取。项目不会自动加载 `.env`，真实 Key 不得提交。文字和视觉请求都关闭深度思考；文字使用流式接口并在客户端内聚合，普通问答限制输出长度。模型名称无效、权限不足或请求超时时会快速进入既有降级流程。

## 测试

```bash
python -m pytest -q
python -m pytest -q tests/test_kitchen_assistant.py
python -m pytest -q tests/test_kitchen_scenario_matrix.py
python -m pytest -q tests/test_qwen_integration.py tests/test_ingredient_vision.py
python -m pytest -q tests/test_skill_manager.py
```

重点覆盖正常流程、多轮自然语言、候选选择、份量缩放、缓存、忌口、厨具、安全问答、计时保护、并行准备和 Schema 故障隔离。

## 已知限制

- 会话只保存在当前 Python 进程，没有 `session_id` 和跨进程恢复。
- 只连接 Mock SDK，不含真实机器人能力 ID。
- 视觉能力仅使用 Mac 默认摄像头识别食材，不判断成熟度、过敏原或食品安全。
- `web_search` 未接通，AI 生成不等于联网搜索。
- 未开启语音功能，只有文字输入。
- 缓存目前按可读菜名组织，尚无过期时间和显式清理命令。

## 下一阶段

开启语音功能、完善会话词典、会话持久化、带引用的官方菜谱搜索。
