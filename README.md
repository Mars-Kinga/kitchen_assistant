# AI 厨房助手 kitchen_assistant

这是一个可在本地运行、可完成完整演示闭环的 Python Robot Skill Runtime。用户输入会经过意图路由和 Skill 状态机，结果再由执行器调用五类模拟机器人能力：语音、屏幕、动作、灯带和表情。

项目目前包含：

- `kitchen_assistant`：支持指定菜名、图片食材盘点、按确认后的食材推荐、候选确认、份量缩放、逐步烹饪、本地计时、局域网状态控制台和完成反馈。

当前是本地模拟版本，不连接真实机器人。千问通过 OpenAI 兼容接口提供可选的 AI 菜谱生成、厨房问答和 Mac 摄像头食材识别；它不等于联网搜索，`OnlineRecipeSearchProvider` 仍是未接通的占位适配层。

## 闭环

当前可验证链路为：

```text
触发 → 理解用户意图 → 执行厨房状态机 → 调用五类 Mock 能力 → 局域网同步状态 → 正常完成或取消
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

### 最快文字演示（推荐评审使用）

文字交互、Mock 机器人五通道和离线/AI 菜谱流程不需要录音、SDL 或 `pygame`。只安装基础与测试依赖即可：

macOS / Linux：

```bash
cd /Users/mars/Desktop/runtime
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

Windows PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

如果虚拟环境已经创建过，只需激活后执行最后三条命令，不需要反复创建或升级 pip。只运行程序可安装 `requirements.txt`；开发、测试使用 `requirements-dev.txt`。

基础安装不再包含 `pygame`。macOS 播放 Edge TTS 生成的 MP3 时直接使用系统自带的 `afplay`，因此不会再因为缺少 `SDL.h` 阻塞安装。

## 可选环境变量

文字模式、离线菜谱和全部默认测试不需要 API Key。

千问 AI 菜谱、问答和视觉识别：
在终端进入文件夹之后，输入：

```bash
export DASHSCOPE_API_KEY='你的API Key'
export QWEN_BASE_URL='https://ws-1jj0fvndfsqmsmid.cn-beijing.maas.aliyuncs.com/compatible-mode/v1'
export QWEN_TEXT_MODEL='qwen3-omni-flash'
export QWEN_VISION_MODEL='qwen3-vl-flash'
export QWEN_TIMEOUT_SECONDS='25'
export QWEN_VISION_TIMEOUT_SECONDS='20'
export QWEN_MAX_RETRIES='0'
```

文字与视觉模型独立配置。文字请求使用 Qwen Omni 的流式接口，视觉请求使用 Qwen VL；两者都显式关闭深度思考。菜谱先查本地固定目录和已验证缓存，本地只要有匹配结果就完全不调用云端；本地没有时，云端一次只生成 1 份包含 6–10 步的完整菜谱。默认不自动重试，非法 JSON 也不会再次请求模型。

局域网控制台会区分生成中与生成失败，并在失败时保留食材、人数和口味设置，提供重试入口。接口返回 401/403 时，请检查 `DASHSCOPE_API_KEY`、`QWEN_BASE_URL` 及模型访问权限；更新配置后需要重启终端服务。此类失败不会再被显示为普通的“没有匹配菜谱”，错误提示不会包含密钥或接口响应正文。

需要显式验证真实千问连接时，运行一次生产客户端检查；它不属于 pytest，也不会在默认测试中联网：

```bash
python scripts/check_qwen_connection.py
```

## 启动入口

`robot_main.py` 是唯一主入口和参数定义来源：

```bash
# 仅测试文字交互（没有局域网控制台）
python robot_main.py

# 仅测试单次文字验证，不播放 TTS
python robot_main.py --no-play 我想做番茄炒蛋

# 仅测试Mac 摄像头食材识别
python robot_main.py --no-play 这是大葱还是小葱

# 同时启动文字模式与局域网控制台（演示时推荐使用这个）
python robot_main.py --console --no-play

# 只运行局域网控制台
python robot_main.py --console-only --no-play
```

控制台默认监听 `0.0.0.0:8765`，启动后会打印本机与局域网访问地址。同一 Wi-Fi 下可用手机、平板或电脑打开。首次使用摄像头时，macOS 会请求相机权限。若拒绝过，可在“系统设置 → 隐私与安全性 → 相机”中为当前终端或 Codex 开启权限。

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
  ingredient_vision.py                # 摄像头状态、图片食材盘点与结果校验
  kitchen_console.py                  # 局域网 HTTP API、静态页面与同源检查
  kitchen_console_static/             # 手机/桌面自适应控制台界面
  mac_camera.py                       # Mac 默认摄像头单帧拍摄和内存压缩
  mock_robot_sdk.py                   # Mock 能力及能力注册表
  voice_io.py                         # 录音、VAD、ASR、TTS
skills/
  hello_skill/
  kitchen_assistant/
    kitchen/                          # 本地确定性状态机与烹饪流程规则
    providers/                        # Mock、AI、缓存、未来搜索适配层
    llm/                              # Prompt、配置、千问文字/视觉客户端
    recipes/
      recipes.json                    # 菜品流程配置（基础菜谱已归档到分类目录）
      catalog/                        # 按类别拆分的人工校订本地菜谱
      sources.json                    # 上游版本、许可证与转换说明
      generated/                      # AI 生成缓存（运行数据）
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

厨房助手会收集必要的人数、口味、忌口、时间、难度和厨具约束。每次先查询本地固定目录和已验证生成缓存，命中后立即使用本地结果，不访问千问；只有本地完全没有匹配菜谱时，AI 才会一次生成一份带完整详情的菜谱。单候选页面可直接说“好”或“开始吧”确认。

### 局域网控制台与图片推荐

控制台每 2 秒读取一次同一进程中的厨房状态，展示机器人模拟状态、摄像头状态、当前菜谱、步骤与计时。照片可以来自手机拍摄、文件上传或 Mac 摄像头单帧；图片只在内存中处理，不写入磁盘，单张限制 8 MB。

多模态模型返回“可见食材”和“不确定物品”两组结果。用户可以删除错误项或手动补充，只有点击“生成菜谱方案”后，确认食材才会写入厨房会话并触发菜谱推荐。控制台没有火源、刀具或高温设备的远程控制接口。

控制台顶部提供本地菜谱搜索入口，可按菜名或食材筛选。点击本地菜谱、推荐候选或当前“做菜进度”卡片，会打开 iOS 风格菜谱 Sheet，展示完整食材、用量与全部步骤；选择人数后可直接选择菜谱并进入做菜流程。

### 视频教程导入

控制台的“把教程变成可执行菜谱”位于“拍下食材”上方，支持粘贴小红书分享文案或上传 MP4/MOV。首版限制 3 分钟、100 MB；后台处理不会改变当前厨房任务。公开移动分享页可以读取时直接获取视频，遇到登录、验证码或不可用内容时提供上传兜底。可选配置 `VIDEO_IMPORT_API_KEY` 接入 Wellbyte；该渠道需要自行具备可用账号及额度。

若桌面网络将小红书域名解析到 `198.18.0.0/15` 的代理占位地址，运行时只对受支持的小红书及 `xhscdn.com` 域名查询固定可信 HTTPS DNS，并将请求固定到验证后的公网 IP，同时保留原域名的 Host 和 TLS SNI；其他域名或私网解析仍会被拒绝。

视频理解复用 `DASHSCOPE_API_KEY` 和 `QWEN_BASE_URL`，使用 `QWEN_VIDEO_MODEL`（默认 `qwen3-omni-flash`），独立请求超时由 `QWEN_VIDEO_TIMEOUT_SECONDS` 配置（默认 90 秒）。基础依赖中的 `imageio-ffmpeg` 提供 FFmpeg；也可使用系统安装的媒体工具。较长视频分段理解后合并，视频的剪辑时间不直接作为烹饪时间。

120 秒以内的单段视频一次生成完整菜谱；多段视频的最终合并只发送文字证据，不重复上传视频。模型流式输出中完整生成的食材和步骤会陆续作为只读预览显示，完整结果仍须校验及用户确认。状态接口的 `metrics` 记录阶段耗时、压缩和模型耗时、首项内容和首步出现时间、请求次数与平台实际返回的 Token 用量；完成后的耗时不随核对时间增长。未知 Token 用量保持空值。

2026-09-17 在本机使用 50 秒“三杯鸡”链接的单次实测：读取 29.4 秒、压缩 20.6 秒、模型 37.2 秒，总计 87.3 秒；66.0 秒出现首项食材，76.0 秒出现首个步骤。一次模型请求使用输入 15,259 / 输出 1,401 Token。这是单个样本，不能作为所有平台和网络条件的延迟保证。后续可评估字幕优先、音频转写与关键帧抽取并行，再一次整理菜谱；尚未启用该路径。

计时一致性修复后，同一链接复测为 74.5 秒（读取 21.3、压缩 19.6、模型 33.6）；52.4 秒出现首项食材、60.0 秒出现首步，输入 15,259 / 输出 2,015 Token。两次均只调用一次模型；耗时差异包含网络与生成波动，不能归因于本地计时修复。这两次测量早于自动 AI 补全调整。

视频未说明的用量、必要做法和烹饪时间由 AI 给出合理建议并标记来源；明确的视频信息和用户修改优先保留。主要肉类尽量提供具体克数或个数，缺少明确量时按人数给 AI 建议；调料等已有“适量”“少量”“按口味”“一圈”表述允许确认。优先在原次视频分析中补全，仅在仍缺少信息时追加一次精简的文字请求，只返回缺失项，不重新上传视频。已有不完整草稿可使用“AI 补全缺失信息”，完成后仍须用户核对和确认；旧的缺少信息提示不会在问题已解决后继续拦截。切配、装盘等无需计时的动作可保持计时空值。

控制台默认一人份，打开详情与选择菜谱沿用当前人数，详情内改人数时按该人数重载用量；已明确的视频份量保留原值。食材显示不会重复拼接已有单位，定性用量不附加多余单位，例如“两勺”“半碗”“适量”。

结果先作为可编辑草稿展示，区分视频提取、AI 补全、用户修改和本地规则调整。修改后先保存并核对整理后的版本，再确认保存到“已导入菜谱”；确认不会再次改变步骤或计时。菜谱保存在 `skills/kitchen_assistant/recipes/imported/`，不提交到 Git；未确认草稿仅在本进程中保留，临时媒体在处理收尾阶段删除。

已导入菜谱复用完整菜谱 Sheet 与机器人教学。存在活动任务时须先结束再开始新菜谱；导入菜谱的计时由用户明确启动，提前进入下一步需要确认。局域网控制台提供计时、完成确认、暂停恢复、食材状态确认及结束任务操作。YouTube 链接首版暂不接入，可以上传相应视频文件。

视频导入接口为 `POST /api/video-imports`（JSON `share_text` 或 multipart `file`）、`GET /api/video-imports/{id}`、`PATCH /api/video-imports/{id}/draft`、`POST /api/video-imports/{id}/complete`、`POST /api/video-imports/{id}/confirm`、`DELETE /api/video-imports/{id}`，以及 `GET /api/imported-recipes`。保存后的菜谱沿用现有详情和选择接口。

真实链接验证单独运行，不属于默认测试，也不会在默认测试中调用云端：

```bash
python scripts/check_video_import.py 'https://xhslink.cn/o/7Al9WMJ4hWr'
```

视频分析返回 401/403 时，需要检查 Key 与服务地址、地域和模型权限是否匹配。可在本地 `config/qwen.env` 填写 `DASHSCOPE_API_KEY` 和 `QWEN_BASE_URL`；该文件被 Git 忽略，非空值优先于启动进程继承的环境变量，内容不会作为脚本执行。更新后重启服务。

### Provider 与缓存边界

- `mock`：本地固定菜谱，包括基础示例和人工校订目录，不在运行时联网。
- `ai_generated`：千问根据需求生成，不带虚构网页 URL。
- `local_cache`：复用此前已通过校验的 AI 菜谱，并按人数缩放用量。
- `web_search`：仅预留，当前不发起真实搜索请求。

AI 会在一次响应中生成一个候选及其完整 6–10 步菜谱，只展示详情校验通过的结果，并保存到 `skills/kitchen_assistant/recipes/generated/`。Prompt 与本地校验共享 `recipe_contract.py` 中的字段和硬约束；语义失败不会自动再次调用模型。生成 JSON 是运行数据，默认被 Git 忽略。

### 本地菜谱目录

本地目录目前有 440 道菜，基础菜谱与扩展菜谱统一存放在 `recipes/catalog/`：肉类、蔬菜、水产、主食与汤，以及新增的轻食与减脂分类。另有 90 道按蔬菜、肉类、水产、主食和汤等类别存放的菜谱参考 HowToCook 固定提交 `c05758fa661ac4efa0361a987b700a351a22159b`。`recipes/recipes.json` 保留菜品流程配置，不再重复保存基础菜谱。HowToCook 菜谱按 Unlicense 使用；每条参考数据保留永久来源链接、提交版本和许可证。所有步骤经过重新表述和本地校订。运行时不抓取或更新上游内容。

修改目录后可执行离线审计：

```bash
python scripts/validate_recipe_catalog.py
```

审计会检查来源版本、许可证、重复 ID/菜名、模糊用量，并对每道导入菜谱执行 1、2、3 人份标准化。完整来源和转换原则见 `skills/kitchen_assistant/recipes/sources.json`。

清理与本地目录同名的 AI 生成缓存时，先预览再执行：

```bash
python scripts/prune_generated_recipe_duplicates.py
python scripts/prune_generated_recipe_duplicates.py --apply
```

### 计时与并行准备规则

- 只有加热、预热、煮炖焖煎烤蒸炸焯炒收汁，以及腌制、浸泡、泡发、静置等步骤保留计时。
- 用户必须说“开始”“下锅了”“开始计时”等才启动当前步骤计时。
- 带时长步骤尚未启动计时时，“下一步”“跳过”或“做好了”不会直接越过。用户必须选择“开始计时”，或明确说“确认完成”表示已自行计时并检查状态。
- 计时只是下限参考；到时后仍需根据完全变色、中心无粉红、蔬菜断生等可观察状态确认，不自动判断熟度。
- 计时运行中要求提前进入下一步时，必须再次确认是否结束计时。
- 腌制/浸泡期间仅在后续存在真实、独立的准备步骤时提供并行建议。建议不能重复当前步骤已经量取或加入的调料，也不能提前热油、空烧锅或接触腌制中的肉。
- 没有合适并行步骤时统一提示：“在这段时间里你可以同步做自己想做的事情，时间到了我会叫你～”。

AI Prompt 与本地筛选使用相同规则；即使模型生成了不合规的并行建议，本地状态机仍会拒绝。

### 能力边界

本项目用于食材盘点、菜谱建议、计时和流程状态展示，不判断食材是否变质、食物是否熟透、是否含过敏原，也不判断燃气、火源、电器或现场环境是否安全。起火、燃气、触电、受伤等事故输入不会得到具体处置方案；助手只说明超出功能范围、暂停当前指导，并建议寻求现场专业人员或当地紧急服务。

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

演示时保留以下证据：控制台三类状态、图片上传与食材确认、菜谱候选、Skill 状态变化、五类 Mock SDK 日志，以及正常完成流程。


## 已知限制

- 没有完成语音输入功能
- 单 Python 进程、单活动会话，没有用户身份、跨进程恢复或持久化会话。
- 视觉能力只盘点 Mac 摄像头或上传图片中的可见食材，不能确认过敏原、食品安全或食物是否已熟。
- 未接通真实网页菜谱搜索。
- AI 内容存在不确定性，图片食材必须由用户确认，菜谱结果仍由本地 Schema 和标准化规则约束。

## 下一阶段计划

1. 增加会话持久化和多个独立计时器。
2. 接入真实机器人与摄像头状态适配器。
3. 在获得官方搜索 API、鉴权和响应样例后实现带来源引用的真实菜谱搜索。
4. 增加缓存版本、过期策略和运行数据清理命令。



更细的厨房 Skill 输入输出和限制见 [skills/kitchen_assistant/SKILL.md](skills/kitchen_assistant/SKILL.md)，模块边界见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。
