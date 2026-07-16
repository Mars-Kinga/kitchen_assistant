# AI 厨房助手 Skill

## 功能

`kitchen_assistant` 是单进程、多轮对话的厨房指导 Skill。它负责理解做菜需求、推荐候选、确认菜谱、标准化用量和步骤、逐步指导、本地计时、安全问答与完成反馈。

目前没有直接控制机器人硬件；返回结构化结果，由 Runtime 的 `RuntimeExecutor` 调用语音、屏幕、动作、灯带和表情能力。

## 触发方式

触发词定义在 `skill.json`，包括“厨房助手”“我想做”“菜谱”“不知道做什么”以及常见菜名和食材。`runtime_core.agent.SkillAgent` 还会识别包含食物词的烹饪问法和库存表达。

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

状态、步骤索引、计时器、并行准备记录和安全确认均由 `kitchen/session_store.py` 本地决定。Provider 或 LLM 不能直接推进状态、结束计时或调用机器人能力。

## 菜谱来源

- `mock`：`recipes/recipes.json` 中的本地示例。
- `ai_generated`：配置 `ARK_API_KEY` 后由豆包 Chat Completions 生成。
- `local_cache`：读取 `recipes/generated/` 中已通过校验的生成菜谱。
- `web_search`：预留模式，当前未接通真实搜索。

AI 生成最多三个候选，并把完整菜谱合并保存为按菜名命名的缓存文件。相同菜名、口味和忌口优先复用缓存；人数变化由 `RecipeNormalizer` 缩放食材和步骤用量。

AI 生成不是网页搜索，不提供虚构 URL。Provider 调用失败时可回退本地 Mock；详情生成失败会保留候选并提示重试或更换，不让无关菜谱静默替换当前选择。

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

- 含生肉或海鲜的菜谱开始前进入 `WAITING_MEAT_THAW`。
- 起火、燃气味、大量浓烟、烫伤和电器进水等风险优先由本地规则处理，并可暂停流程。
- 肉类计时只是参考下限，必须检查完全变色、中心无粉红或使用合适温度计。
- 助手不声称看见现场，不保证食物已熟。
- 无候选、Provider 超时、AI JSON 错误、缓存写入失败和问答服务不可用均有本地降级路径。
- Skill 异常或输出不符合 Schema 时，`SkillManager` 会结束活动会话并返回五通道安全错误，不影响其他 Skill。

## 依赖

运行依赖由项目根目录 `requirements.txt` 管理，测试依赖由 `requirements-dev.txt` 管理。主要外部包包括：

- `openai`：可选豆包 Chat Completions；
- `jsonschema`：Manifest 引用的输入/输出契约校验；
- `numpy`、`scipy`、`sounddevice`：语音采集链路；
- `edge-tts`、`pyttsx3`、`pygame`：语音播放链路。

厨房文字流程和默认测试不需要网络或 Key。

## 配置

豆包：

- `ARK_API_KEY`
- `DOUBAO_BASE_URL`（可选）
- `DOUBAO_MODEL`（可选）

Key 只从环境变量读取。项目不会自动加载 `.env`，真实 Key 不得提交。

## 测试

```bash
python -m pytest -q
python -m pytest -q tests/test_kitchen_assistant.py
python -m pytest -q tests/test_kitchen_scenario_matrix.py
python -m pytest -q tests/test_skill_manager.py
```

重点覆盖正常流程、多轮自然语言、候选选择、份量缩放、缓存、忌口、厨具、安全问答、计时保护、并行准备和 Schema 故障隔离。

## 已知限制

- 会话只保存在当前 Python 进程，没有 `session_id` 和跨进程恢复。
- 只连接 Mock SDK，不含真实机器人能力 ID。
- 无视觉能力，不能观察食材状态。
- `web_search` 未接通，AI 生成不等于联网搜索。
- 未开启语音功能，只有文字输入。
- 缓存目前按可读菜名组织，尚无过期时间和显式清理命令。

## 下一阶段

开启语音功能、完善会话词典、会话持久化、带引用的官方菜谱搜索。
