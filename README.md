# AI 厨房助手 kitchen_assistant

这是一个可在本地运行、可完成完整演示闭环的 Python Robot Skill Runtime。用户输入会经过意图路由和 Skill 状态机，结果再由执行器调用五类模拟机器人能力：语音、屏幕、动作、灯带和表情。

项目目前包含：

- `kitchen_assistant`：支持指定菜名、按现有食材推荐、候选确认、份量缩放、逐步烹饪、本地计时、并行准备、安全问答和完成反馈。

当前是本地模拟版本，不连接真实机器人。豆包 Chat Completions 用于可选的 AI 菜谱生成，不等于联网搜索；`OnlineRecipeSearchProvider` 仍是未接通的占位适配层。

## 闭环

当前可验证链路为：

```text
触发 → 理解用户意图 → 执行厨房状态机 → 调用五类 Mock 能力 → 给出反馈 → 正常完成或安全取消
```

典型状态流转：

```text
IDLE
  → COLLECTING_REQUEST / COLLECTING_PREFERENCES
  → SEARCHING_RECIPES
  → PRESENTING_CANDIDATES
  → WAITING_RECIPE_CONFIRMATION
  → WAITING_MEAT_THAW（含生肉时）
  → COOKING
  → COMPLETED / CANCELLED
```

`PAUSED` 用于烹饪中暂停；活动计时会冻结，恢复后从剩余时间继续。

## 环境与安装

建议使用 Python 3.11–3.13。当前本地验证环境为 Python 3.13.9，CI 会覆盖 3.11、3.12 和 3.13。

macOS / Linux：

```bash
cd /Users/mars/Desktop/runtime
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements-dev.txt
```

Windows PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements-dev.txt
```

只运行程序可安装 `requirements.txt`；开发、测试使用 `requirements-dev.txt`。依赖均设置了兼容版本上限，避免无界升级。GitHub Actions 配置位于 `.github/workflows/test.yml`。

## 可选环境变量

文字模式、离线菜谱和全部默认测试不需要 API Key。

豆包 AI 菜谱生成：
在终端进入文件夹之后，输入：

```bash
export ARK_API_KEY='你的API Key'
export DOUBAO_BASE_URL='https://ark.cn-beijing.volces.com/api/v3'   # 仅为例子，根据情况可选
export DOUBAO_MODEL='doubao-seed-2-0-mini-260428'                   # 仅为例子，根据情况可选
export DOUBAO_TIMEOUT_SECONDS='90'                                 # 可选，交互请求超时秒数
export DOUBAO_MAX_RETRIES='0'                                     # 可选，超时后的 SDK 重试次数
```

模型名称不写死在业务代码中。评审或部署时可以通过 `DOUBAO_MODEL` 切换为当前账号有权限、且兼容 Chat Completions 的模型。默认交互超时为 90 秒且不自动重试，为响应较慢的模型保留生成时间，同时避免 SDK 自动重试进一步延长等待。

需要显式验证真实豆包连接时，运行一次生产客户端检查；它不属于 pytest，也不会在默认测试中联网：

```bash
python scripts/check_doubao_connection.py
```

## 启动入口

`robot_main.py` 是唯一主入口和参数定义来源：

```bash
# 文字交互
python robot_main.py

# 单次文字验证，不播放 TTS
python robot_main.py --no-play 我想做番茄炒蛋

```

旧入口保留为兼容包装，不再维护重复的路由和执行代码：

```bash
python text_input.py --no-play 你好      # 委托 robot_main.main
```

未来扩展语音输入时，录音、VAD、ASR 和 TTS 继续放在 `runtime_core/voice_io.py`；`robot_main.py` 只负责组合输入模式与统一 Runtime，不承载厨房业务规则。

## 目录与调用关系

```text
robot_main.py                         # 主入口、文字/语音循环、普通聊天回退
text_input.py / voice_input.py        # 兼容入口，仅委托主入口
chat_handler.py                       # 普通聊天的五个通道反馈
runtime_core/
  agent.py                            # 本地触发词和烹饪意图路由
  skill_manager.py                    # Manifest、能力、Schema、加载与故障隔离
  executor.py                         # 五通道反馈执行与逐通道降级
  mock_robot_sdk.py                   # Mock 能力及能力注册表
  voice_io.py                         # 录音、VAD、ASR、TTS
skills/
  hello_skill/
  kitchen_assistant/
    kitchen/                          # 本地确定性状态机与安全规则
    providers/                        # Mock、AI、缓存、未来搜索适配层
    llm/                              # Prompt、配置、豆包客户端
    recipes/                          # 本地菜谱与生成缓存
    schemas/                          # 输入、输出 JSON Schema
tests/                                # 离线单元、场景矩阵和入口回归
```

主调用链：

```text
robot_main.handle_input
  → SkillManager.run_user_text
  → Skill scripts/run.py
  → KitchenSession（厨房 Skill）
  → RuntimeExecutor.execute_plan
  → MockRobotSDK + VoiceOutput
```

## Skill 契约与故障隔离

启动时 `SkillManager` 会逐个检查：

- `skill.json` 必填字段和字段类型；
- Skill 名称是否重复；
- 入口和 Schema 是否位于自己的 Skill 目录内且真实存在；
- JSON Schema 本身是否有效；
- `required_capabilities` 是否由当前 Runtime 提供。

坏 Manifest、缺入口或缺能力只会禁用对应 Skill，并输出 `[Skill 加载警告]`，不会阻断其他 Skill。调用前校验输入，调用后及异步计时事件返回时校验输出；执行异常或输出违约会转为安全的五通道错误反馈并结束该会话。

每个可执行反馈必须包含：

- `speech` 或 `question`
- `display`
- `robot_action`
- `led_effect`
- `expression`

`RuntimeExecutor` 会独立执行每个通道。未知动作、灯效、表情或单通道异常会安全降级，不会遮蔽其他反馈。

## 厨房助手功能

### 两种发起方式

指定菜名：

```text
我想做番茄炒蛋
```

按食材推荐：

```text
我不知道做什么，我有胡萝卜和鸡肉
```

厨房助手会收集必要的人数、口味、忌口、时间、难度和厨具约束。指定菜名时生成一个完整候选，此时直接说“好”“可以”或“开始吧”即可确认，不必再说“第一个”；按现有食材推荐时展示最多三个候选，必须先选第几个或菜名，再明确确认。“好”在多候选页面不会擅自替用户选择。

### Provider 与缓存边界

- `mock`：本地示例菜谱，不冒充网页结果。
- `ai_generated`：豆包根据需求生成，不带虚构网页 URL。
- `local_cache`：复用此前已通过校验的 AI 菜谱，并按人数缩放用量。
- `web_search`：仅预留，当前不发起真实搜索请求。

AI 会在一次响应中同时生成候选和完整菜谱，只展示详情校验通过的候选，并保存到 `skills/kitchen_assistant/recipes/generated/`。Prompt 与本地校验共享 `recipe_contract.py` 中的字段和硬约束；语义失败不会自动再次调用模型。生成 JSON 是运行数据，默认被 Git 忽略。

### 计时与并行准备安全规则

- 只有加热、预热、煮炖焖煎烤蒸炸焯炒收汁，以及腌制、浸泡、泡发、静置等步骤保留计时。
- 用户必须说“开始”“下锅了”“开始计时”等才启动当前步骤计时。
- 带时长步骤尚未启动计时时，“下一步”“跳过”或“做好了”不会直接越过。用户必须选择“开始计时”，或明确说“确认完成”表示已自行计时并检查状态。
- 计时只是下限参考；到时后仍需根据完全变色、中心无粉红、蔬菜断生等可观察状态确认，不自动判断熟度。
- 计时运行中要求提前进入下一步时，必须再次确认是否结束计时。
- 腌制/浸泡期间仅在后续存在真实、独立且安全的准备步骤时提供并行建议。建议不能重复当前步骤已经量取或加入的调料，也不能提前热油、空烧锅或接触腌制中的肉。
- 没有合适并行步骤时统一提示：“在这段时间里你可以同步做自己想做的事情，时间到了我会叫你～”。

AI Prompt 与本地筛选使用相同规则；即使模型生成了不合规的并行建议，本地状态机仍会拒绝。

### 食品安全

含生鲜肉类、鱼虾蟹贝的菜谱在开始前会确认它是新鲜食材，或在来自冷冻时已经完全解冻；鸡蛋、奶制品等不会触发这项检查。起火、燃气味、大量浓烟、烫伤、电器进水等风险优先由本地规则处理，输出停止动作和警示反馈，并可暂停烹饪。助手不声称看见现场，也不以计时替代温度或熟度检查。

## 演示流程

```text
我不知道做什么，我有胡萝卜和鸡肉
1人
正常
第二个
开始
没有解冻
解冻好了
完成了
开始计时
（计时到或按提示确认）
（测试的时候如果不想等待计时，可以直接说“我做好了，下一步”，机器人会询问是否结束计时，再“确认”即可进入下一步）
……
下一步
谢谢
再见
```

演示时保留以下证据：Skill 路由结果、状态变化、五类 Mock SDK 日志、正常完成，以及至少一个异常/安全分支（例如未启动计时就说“下一步”）。


## 已知限制

- 没有完成语音输入功能
- 单 Python 进程、单活动会话，没有用户身份、跨进程恢复或持久化会话。
- 无视觉输入，不能观察锅内状态或确认食物已熟。
- 未接通真实网页菜谱搜索。
- AI 内容存在不确定性，最终步骤仍由本地 Schema、标准化和安全规则约束。

## 下一阶段计划

1. 将语音输入抽象成独立输入适配器，支持可打断播报和异步计时提醒。
2. 在获得官方搜索 API、鉴权和响应样例后实现带来源引用的真实菜谱搜索。
3. 增加缓存版本、过期策略和运行数据清理命令。
4. 增加情绪价值提供，回答的语气等再进行优化。
5. 考虑将机器人命名为“饭宝”，增加“饭宝”的性格。



更细的厨房 Skill 输入输出和限制见 [skills/kitchen_assistant/SKILL.md](skills/kitchen_assistant/SKILL.md)，模块边界见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。
