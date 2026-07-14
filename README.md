# 厨房助手

> 项目名称：**厨房助手**。它构建在下方完整保留的 Python Robot Skill Runtime 初始版本之上。

## 当前版本说明

这是一个本地可运行的 Python 机器人 Skill Runtime：文字或语音输入先经过 Skill 路由，Skill 返回结构化反馈后由 `RuntimeExecutor` 实际调用 `MockRobotSDK` 的语音、屏幕、动作、灯带和表情模拟能力。

当前项目包含 `hello_skill` 与厨房助手。默认使用明确标识的本地示例菜谱；可选配置豆包 Chat Completions 后，系统会生成候选和结构化菜谱。豆包在本项目中是 **AI 生成**，不是联网菜谱搜索；项目不连接真实机器人。

AI 首次生成的候选及其通过本地校验的完整菜谱会自动保存到 `skills/kitchen_assistant/recipes/generated/`（最多三个候选各一份）。首次请求会为三个候选各生成一次详情，后续在人数、口味与忌口条件相同且菜名匹配时，会优先读取这些本地缓存，`provider_mode` 为 `local_cache`，不会再次调用模型生成菜谱。缓存写入失败不会影响当前烹饪流程。

## 真实结构

```text
robot_main.py                         # 文本/语音入口、普通聊天 fallback
runtime_core/                         # 路由、Skill 管理、执行器与 Mock 硬件
skills/hello_skill/                   # 原始问候示例
skills/kitchen_assistant/
  kitchen/                            # 状态机、请求解析、推荐、标准化、问答
  providers/                          # Mock / Online / Agent 菜谱 Provider 适配层
  llm/                                # 豆包 Chat Completions 客户端与 Prompt
  recipes/recipes.json                # 本地回退菜谱和离线示例菜谱
  schemas/                            # Skill 输入/输出结构约定
tests/test_kitchen_assistant.py       # 完全离线测试
docs/ARCHITECTURE.md
docs/MISSING_INPUTS.md
```

`robot_main.py → SkillManager → Skill 脚本 → RuntimeExecutor → MockRobotSDK` 是主链路。`SkillManager` 缓存 Skill 模块；活动厨房 Skill 会保留单进程会话，因此“下一步”“第一个”“开始吧”等短句仍会送入厨房助手。

## 运行与测试

```bash
pip install -r requirements.txt
python robot_main.py
python robot_main.py --no-play "你好"
python robot_main.py --no-play "我想做番茄鸡蛋面"
python robot_main.py --no-play "我不知道晚饭做什么"
python -m pytest -q
pytest -q
python -m compileall -q .
```

`pytest.ini` 已将项目根目录加入测试导入路径，所以两种 pytest 命令都可用。语音模式仍沿用现有命令：`python robot_main.py --voice` 或 `python robot_main.py --voice --manual --no-play`。语音识别是原有 MiMo 链路，可能需要其既有 API Key；厨房主流程、文字演示和测试均不需要网络或 Key。

## 厨房助手能力

### 指定菜名

用户可说“我想做番茄鸡蛋面”“教我做可乐鸡翅”。系统提取菜名、人数、口味、忌口、时间、难度和厨具等已给出的信息，只逐项询问缺少的基本人数与口味，然后生成或检索最多三个候选。配置豆包时优先使用 `DoubaoAIRecipeProvider`；未配置或调用失败时自动回退 `MockRecipeSearchProvider`。用户必须选中候选并明确说“可以 / 开始吧 / 就这个 / 按这个做”后，才进入 `COOKING`。

只说菜名时，系统把食材库存视为“未提供”，不会擅自显示“缺牛排 / 缺番茄”等购物清单；候选会显示“食材见详情”，确认后再给出完整用量。若要按库存判断缺项，请明确说“我有牛排、橄榄油、黄油、盐和黑胡椒”。

### 按食材推荐

用户说“不知道晚饭做什么”后，机器人询问已有食材。例如“我有鸡蛋、番茄和面条”，会优先推荐食材匹配率更高、缺少主要食材更少、时间和难度更符合偏好的菜谱。支持“第一个 / 第二个 / 菜名 / 换一批 / 更简单一点 / 更快一点”。

离线 Mock Provider 包含番茄炒蛋、番茄鸡蛋面、简单汤面、青菜鸡蛋面、蛋炒饭、土豆丝、可乐鸡翅、咖喱饭和牛肉面。离线来源在内部清晰标为“本地示例菜谱”或“Mock Recipe Provider”，URL 为 `null`；不冒充联网结果。AI 生成数据在内部保留来源用于调试，但用户侧结果不输出 `source_name: 豆包 AI 生成`，仅通过 `provider_mode=ai_generated` 区分模式，也不冒充网页来源。

### 烹饪、问答与安全

状态机为：`IDLE`、`COLLECTING_REQUEST`、`COLLECTING_INGREDIENTS`、`COLLECTING_PREFERENCES`、`SEARCHING_RECIPES`、`PRESENTING_CANDIDATES`、`WAITING_RECIPE_CONFIRMATION`、`WAITING_MEAT_THAW`、`COOKING`、`PAUSED`、`COMPLETED`、`CANCELLED`。

烹饪中保留原有下一步、上一步、重复、暂停、恢复、当前进度、本地计时、退出功能。暂停烹饪时活动计时器会冻结，恢复后从原剩余时间继续。规则式问答可处理小锅放不下长面条、面条粘连、水量、白糖/葱替代、火候、锅太小和油溅等问题，回答后不改变当前步骤。大量浓烟、锅里起火、燃气味等安全问题会暂停指导，并发出 `stop + red + warning` 模拟反馈。

厨房会话也提供陪伴式反馈：确认一人食时会给出温和鼓励；用户说“切好了 / 炒完了 / 做好了 / OK了 / 搞定了”等明确完成语句时会鼓励并自动进入下一步；烹饪中单独说“好的”只表示收到，不改变当前步骤；“谢谢”也只回复陪伴，不推进步骤；完成后会以彩虹灯效庆祝。候选确认阶段仍支持“好 / 好的 / 行 / 没问题”，不再要求用户重复说固定确认语。灯带会依据准备、调味、加热、鼓励和完成等事件使用 SDK 已提供的蓝、暖白、黄、绿和彩虹效果。AI 生成菜谱会要求按人数给出食材数量、油盐和常见调味的明确建议，并在炒制时说明热锅、下油、补油和调味顺序。

陪伴、感谢、等待、步骤鼓励和完成文案集中维护在 `kitchen/response_phrases.py`，不会混进菜谱 JSON 或状态机。每次从对应文案组随机选择，并避免连续两次使用同一句。AI 候选的完整菜谱若生成或校验失败，会保留原菜名并提示“重试 / 换一个”，不会再静默回退成番茄炒蛋等无关菜谱。

含生肉或海鲜的菜谱在明确开始前会进入 `WAITING_MEAT_THAW`，询问食材是否已完全解冻。未解冻时，助手会说明安全的快速做法：优先用微波炉解冻档分段解冻并立刻烹饪；没有微波炉时使用密封冷水浸泡、每 30 分钟换水。不能用室温台面或热水解冻。`RecipeNormalizer` 会删除模型菜谱中重复的解冻步骤，确认完成后不会再次要求解冻。

步骤计时只保留给依赖火候的操作，例如煮、炖、焖、煎、烤、蒸、炸、焯和收汁。淘米、洗菜、切菜、打蛋、搅拌、调味和装盘即使模型返回了时长，本地标准化层也会清除该计时。

牛排会在收集人数后额外询问三分、五分、七分或全熟。AI 菜谱要求把正反面煎制拆成带 `duration_seconds` 的步骤：用户说“开始煎”后才启动正面计时，时间到时提醒翻面；用户确认“翻面好了”后启动另一面计时，结束后提示静置。文字交互模式会主动输出到时提醒。

牛排还会询问厚度。普通约 2 厘米的牛排，单面计时会被限制为 30–90 秒的**初始参考**，不会默认给出“两分钟一面”；实际熟度仍须根据中心温度、上色和切面检查调整。完整菜谱会列出牛排、耐高温油（可选精炼橄榄油）、盐、黑胡椒，并给出黄油、蒜和迷迭香等可选增香材料。

`RecipeNormalizer` 把 Provider 的 RawRecipe 变为统一 Recipe；它会校验菜名、食材与步骤，连续编号，并依据“切 / 搅拌 / 倒入 / 翻炒 / 装盘”等关键词补齐本地 Mock 动作、灯带、表情和屏幕文案。标准化或 Provider 详情异常时会尝试本地番茄炒蛋回退；开始前还会由本地规则复核用户忌口，回退菜谱若与过敏或忌口冲突会被拒绝。复杂、且未命中本地规则的普通烹饪问题可交给豆包回答；起火、燃气味、大量浓烟、烫伤和电器进水始终先由本地安全规则处理。

标准化层还会把过密的备菜说明拆成容易记忆的小步骤，例如“腌肉”“泡发木耳”“切配蔬菜和姜蒜”分别指导。对于明显不合理的 AI 炒制时长，本地规则会设置保守下限：肉丝至少 120 秒并要求观察完全变色、中心无粉红；蔬菜炒至变软至少 180 秒，并明确以实际断生和软度为准，而不是把计时当成熟度保证。

## 多模态 Mock 反馈

每个厨房有效回复都有 `speech` 或 `question`、`display`、`robot_action`、`led_effect`、`expression`。`RuntimeExecutor` 会逐项调用 Mock SDK，并打印类似：

```text
[模拟SDK-灯带] 蓝色动态效果 | effect=blue_dynamic
[模拟SDK-表情] focused
[模拟SDK-动作] 鼓励手势 | action=encourage_gesture
[模拟SDK-屏幕] 步骤 1/3：锅中加水烧开
[模拟SDK-语音请求] 锅中加水烧开，放入面条并轻轻搅动。
```

厨房层只使用老师提供的 Mock SDK 已有动作和灯带值，例如 `nod`、`show_concern`、`encourage_gesture`、`high_five`、`yellow`、`green_dynamic`。执行器遇到未知动作会降级为 `idle_wait`，未知灯带降级为 `white`，未知/缺失表情降级为 `neutral`；某一项 Mock 调用失败不会阻断其余能力。

## 两套演示脚本

已知菜名：

```text
我想做番茄鸡蛋面
一个人吃，清淡一点
第一个
可以，开始吧
下一步
我拿了个小锅，但是面太长放不进去，可以先让下面软化再慢慢压进去吗？
下一步
帮我计时十秒
还有多久
暂停一下
继续
我做到哪一步了
退出厨房助手
```

按食材推荐：

```text
我不知道晚饭做什么
我有鸡蛋、番茄和面条
一个人，少盐
第二个
换一个更简单的
就这个，开始吧
下一步
再说一遍
我没有葱，可以不放吗？
下一步
退出厨房助手
```

每轮都可以在控制台看到五类反馈。文字交互模式有本地后台轮询，到时会主动打印并执行计时提醒；语音模式仍是逐轮录音，无法在一次录音过程中即时打断播报。会话也仅限单用户、单 Python 进程。

## 扩展点与限制

`OnlineRecipeSearchProvider` 是 `web_search` 占位适配层，不发 HTTP 请求、不抓取网页。火山方舟官方文档已经提供 Responses API、Web Search 和 Function Calling 的能力入口，但本项目还没有确认当前账号是否已开通组件、当前模型是否支持、以及实际请求和引用字段，因此不会直接猜测并启用。`AgentRecipeProvider` 也只是未来扩展位置。`DoubaoLLMClient` 使用普通 Chat Completions，只有在环境变量中设置 Key 后才启用。所有情况下，状态、步骤、计时与安全规则仍由本地程序决定。

要新增离线菜谱，编辑 `skills/kitchen_assistant/recipes/recipes.json`，提供菜名、来源标识、食材、步骤、预计时间、难度和厨具；`RecipeNormalizer` 会补齐机器人反馈。真实菜谱搜索、豆包 Agent、真实机器人 SDK 所需资料与边界见 [docs/MISSING_INPUTS.md](docs/MISSING_INPUTS.md)。整体模块关系见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

## 豆包 Chat Completions（可选）

项目已接入普通 Chat Completions：`openai` SDK、Base URL `https://ark.cn-beijing.volces.com/api/v3`、模型 `doubao-seed-2-0-mini-260428`。密钥读取集中在 `skills/kitchen_assistant/llm/config.py`，安装依赖后，只有设置 `ARK_API_KEY` 才会启用 AI 生成；没有 Key、调用超时、鉴权失败、空结果或 JSON 校验失败都会自动使用本地 Mock 数据，厨房助手不会中断。

macOS zsh 可在当前终端临时设置：

```bash
export ARK_API_KEY='你的真实 Key'
export DOUBAO_BASE_URL='https://ark.cn-beijing.volces.com/api/v3'  # 可选，已有默认值
export DOUBAO_MODEL='doubao-seed-2-0-mini-260428'                  # 可选，已有默认值
python scripts/test_doubao_connection.py
```

连接脚本只做一次纯文本 Chat 测试，默认 pytest 不会调用真实网络。项目**不会自动读取** `.env` 文件；[doubao.env.example](config/doubao.env.example) 是可复制参考，复制后的私有文件也需要你通过终端、IDE 环境配置或安全的环境管理工具导出变量。真实 Key 绝不能放进 `README.md`、代码、测试或示例文件。

启用后，豆包用于：动态候选菜谱、选中后的完整结构化菜谱、以及本地规则未覆盖的普通烹饪问答。每个 AI 菜谱仍会经过 `RecipeNormalizer`，由本地补齐机器人反馈；用户仍须明确确认才会开做，豆包不能推进步骤、控制计时器或调用 SDK。用返回元数据的 `provider_mode` 查看当前来源：`ai_generated` 为豆包生成，`mock` 为本地示例，`web_search` 仅为尚未接通的预留模式。

这不等于联网搜索：普通 Chat Completions 没有在本项目中被当作网页浏览或真实菜谱来源使用，因此 AI 生成菜谱不带网页 URL。若要接入真实搜索 API，请按 [docs/MISSING_INPUTS.md](docs/MISSING_INPUTS.md) 提供官方资料。

## 初始版本 README（完整保留）

以下内容来自你上传的初始 README，原文保留，用于记录项目最初的 Runtime 能力与边界；上方“当前版本说明”才是厨房助手的最新功能说明。

# Skill Runtime 初步运行环境

这是一个最小版的文字 / 语音交互运行环境，用来演示：

1. 启动时扫描 `skills/` 目录；
2. 根据用户输入选择 Skill；
3. 未命中 Skill 时走普通聊天；
4. 命中 Skill 时调用示例 Skill；
5. 支持文字输入、语音输入、语音识别、语音合成和 VAD 自动截断录音。

当前只保留一个简单示例 Skill：`hello_skill`。

---

## 目录结构

```text
runtime/
  robot_main.py
  chat_handler.py
  runtime_core/
    agent.py
    skill_manager.py
    executor.py
    voice_io.py
    audio_in.py
    logger.py
  skills/
    hello_skill/
      skill.json
      SKILL.md
      schemas/
      scripts/
        run.py
```

---

## 安装依赖

```powershell
cd E:\智元机器人\skill-1\runtime
pip install -r requirements.txt
```

语音识别使用 MiMo ASR，需要设置 API Key：

```powershell
$env:MIMO_API_KEY="你的 tp-... key"
```

如果只想测试文字模式，不需要设置 API Key。

---

## 运行方式

文字交互：

```powershell
python robot_main.py
```

单次文本输入：

```powershell
python robot_main.py "你好"
```

语音自动监听模式，说话自动开始，停顿后自动截断：

```powershell
python robot_main.py --voice
```

语音手动模式，每轮按 Enter 开始录音：

```powershell
python robot_main.py --voice --manual
```

只识别和打印，不播放 TTS：

```powershell
python robot_main.py --voice --no-play
```

查看麦克风设备：

```powershell
python robot_main.py --list-devices
```

指定麦克风设备：

```powershell
python robot_main.py --voice --device 1
```

---

## 当前示例 Skill

`hello_skill` 是一个问候 Skill。

触发词：

```text
你好
您好
打招呼
问候
hello
```

说“你好”后，会输出：

```text
你好，我收到了你的输入：你好。这是一个示例 Skill 的回复。
```

---

## 当前边界

- 只保留一个示例 Skill；
- 不接真实机器人 SDK；
- 不做动作 SDK、动作映射、运控、导航或底层运动控制；
- 语音输入链路为：麦克风录音 -> VAD 自动截断 -> MiMo ASR 转文字 -> Skill/普通聊天 -> TTS 播放。

---

## 模拟机器人 SDK

当前运行环境新增 `runtime_core/mock_robot_sdk.py`，用于模拟机器人硬件能力。它不会调用真实机器人，只在控制台输出日志，方便验证 Skill 输出是否能触发正确能力。

支持的模拟能力包括：

- 动作：`wave_hand`、`handshake`、`fist_bump`、`high_five`、`nod`、`hug`、`turn_left`、`turn_right`、`step_forward`、`step_back`、`stop` 等。
- 灯带：`white`、`blue`、`green`、`yellow`、`red`、`warm_white`、`green_dynamic`、`blue_dynamic`、`rainbow` 等。
- 屏幕：通过 `display` 字段显示文本。
- 表情：通过 `expression` 字段输出模拟表情日志。
- 语音：通过 `speech` 或 `question` 字段调用 TTS 播报。

Skill 输出示例：

```json
{
  "speech": "太棒了，连续答对三个，来碰拳！",
  "robot_action": "fist_bump",
  "led_effect": "green_dynamic",
  "expression": "happy",
  "display": "连续答对 3 个"
}
```
