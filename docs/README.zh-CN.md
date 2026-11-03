# AI 厨房助手 kitchen_assistant

这是一个可在本地运行、可完成完整演示闭环的 Python Robot Skill Runtime。用户输入会经过意图路由和 Skill 状态机，结果再由执行器调用五类模拟机器人能力：语音、屏幕、动作、灯带和表情。

项目目前包含：

- `kitchen_assistant`：支持指定菜名、图片食材盘点、按确认后的食材推荐、候选确认、份量缩放、逐步烹饪、本地计时、局域网控制台、视频/图文教程导入和完成反馈。

当前是本地模拟版本，不连接真实机器人。千问通过 OpenAI 兼容接口提供可选的 AI 菜谱生成、厨房问答和 Mac 摄像头食材识别；它不等于联网搜索，`OnlineRecipeSearchProvider` 仍是未接通的占位适配层。

## 开发与文档

本说明对应仓库当前实现。项目包含运行框架、菜谱推荐、计时、视觉、控制台、教程导入和便携启动；共享说明见 [架构](../docs/ARCHITECTURE.md)、[配置与待补资料](../docs/MISSING_INPUTS.md) 和 [新电脑运行指南](../START_HERE.md)。

本地 `doc/` 保存编号需求和模板，按项目约定被 Git 忽略，不随仓库或迁移包分发。源码仓库中的 `AGENTS.md` 记录协作规则；缺少本地需求目录时，先使用共享说明及对应代码。

## 闭环

当前可验证链路为：

```text
触发 → 理解用户意图 → 执行厨房状态机 → 调用五类 Mock 能力 → 局域网同步状态 → 正常完成或取消
```

典型状态流转：

```text
IDLE
  → COLLECTING_REQUEST / COLLECTING_INGREDIENTS / COLLECTING_PREFERENCES
  → SEARCHING_RECIPES
  → PRESENTING_CANDIDATES
  → WAITING_RECIPE_CONFIRMATION
  → WAITING_MEAT_THAW（含需确认状态的肉类或水产时）
  → COOKING
  → COMPLETED / CANCELLED
```

`PAUSED` 用于烹饪中暂停；活动计时会冻结，恢复后从剩余时间继续。

## 迁移到其他电脑（含 Windows）

可交付包为 `dist/kitchen-assistant-portable.zip`。新电脑安装 64 位 Python 后，解压、双击 `install.cmd` 安装，再双击 `start.cmd` 启动。完整操作见 [START_HERE.md](../START_HERE.md)。默认端口改为 18765，占用时自动尝试后续 19 个端口；包内 `config/console.json` 可设置首选端口。

重新打包（包含本机生成和导入菜谱，排除密钥与日志）：

```bash
python scripts/build_portable.py --include-local-recipes
```

## 环境与安装

项目 CI 配置覆盖 Python 3.11、3.12 和 3.13；迁移安装脚本接受 3.11–3.14，推荐使用 3.13。2026-11-03 在 macOS / Python 3.14.6 上完成 487 项 Python 测试与三组 Node 页面逻辑检查，全部通过。440 道菜谱审计、文字启动、迁移包解压后的首页与状态接口检查也通过。未复测真实云端、硬件、Windows 安装或浏览器人工操作；安装器接受某版本不等于该版本已通过所有平台验证。

### 最快文字演示（推荐评审使用）

文字交互、Mock 机器人五通道和离线/AI 菜谱流程不需要录音、SDL 或 `pygame`。只安装基础与测试依赖即可：

macOS / Linux：

```bash
cd kitchen_assistant
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

如果虚拟环境已经创建过，只需激活后安装所需依赖并运行测试，不需要反复创建或升级 pip。只运行程序可安装 `requirements.txt`；开发、测试使用 `requirements-dev.txt`。

基础安装不再包含 `pygame`。macOS 播放 Edge TTS 生成的 MP3 时直接使用系统自带的 `afplay`，因此不会再因为缺少 `SDL.h` 阻塞安装。

## 可选模型配置

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

也可把 `config/qwen.env.example` 复制为 `config/qwen.env` 后填写。程序读取这个文件中允许的七个千问配置项（即上例中的变量），非空值覆盖同名环境变量；不会自动加载根目录 `.env`，也不会将配置文件作为脚本执行。示例地址和模型名称须与自己的账号权限匹配，修改后重启服务。

文字与视觉模型独立配置，当前默认分别为 `qwen3-omni-flash`、`qwen3-vl-flash`，两者都显式关闭深度思考，文字请求使用流式接口。菜谱默认优先使用本地目录和已验证缓存；显式要求绕过缓存的请求除外。按菜名生成一个完整候选；仅按食材推荐时允许 1–3 个完整候选，优先两份。

`QWEN_MAX_RETRIES` 默认 0，控制客户端重试；业务层另有最多一次定向纠错。遇到非法 JSON 或可纠正的校验失败时，先保留完整有效候选；没有可用结果且剩余预算允许时才纠错。“默认零重试”不代表整个任务一定只有一次模型请求。

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

控制台默认监听 `0.0.0.0:18765`，启动后会打印本机与局域网访问地址。同一 Wi-Fi 下可用手机、平板或电脑打开。首次使用摄像头时，macOS 会请求相机权限。若拒绝过，可在“系统设置 → 隐私与安全性 → 相机”中为当前终端或 Codex 开启权限。

旧入口保留为兼容包装，不再维护重复的路由和执行代码：

```bash
python text_input.py --no-play 你好      # 委托 robot_main.main
```

### 可选语音模式

已有 `--voice` 入口、VAD 录音、ASR 转写和 TTS 播放代码；实际麦克风、识别服务和连续对话体验仍需在目标设备验证。安装 `requirements-voice.txt` 后可使用：

```bash
python robot_main.py --voice
python robot_main.py --voice --manual
```

ASR 使用 `MIMO_API_KEY`（或 `TOKEN_PLAN_API_KEY`）、`MIMO_BASE_URL` 和 `MIMO_ASR_MODEL`，与菜谱的千问配置独立。TTS 支持 Edge 与本地 `pyttsx3`；`--no-play` 关闭播放。语音模式会将录音写入 `voice_outputs/`，不适用图片识别的“仅内存处理”说明。

录音、VAD、ASR 和 TTS 位于 `runtime_core/voice_io.py`；`robot_main.py` 组合输入模式并将转写结果送入同一 Runtime，不承载厨房业务规则。

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
  video_imports.py                     # 视频/图文导入、草稿、来源与指标
  video_media.py / video_network.py    # 媒体处理与受限公网下载
  recipe_post_media.py                 # 图文图片准备
  video_recipe_store.py                # 已确认导入菜谱持久化
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
      imported/                       # 用户确认的导入菜谱（运行数据）
    schemas/                          # 输入、输出 JSON Schema
tests/                                # 离线单元、场景矩阵和入口回归
scripts/                              # 迁移、目录审计与显式联网检查
config/                               # 配置示例与被忽略的本机配置
docs/                                 # 共享架构与接入说明
doc/                                  # 本地编号需求，不提交 Git
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

厨房助手收集人数、口味、忌口、时间、难度和厨具约束。默认优先复用本地固定目录和已验证缓存；未命中或明确绕过缓存时才请求 AI。按菜名生成一个候选；仅按确认食材推荐时优先两份完整方案、最多三份，有一份可行方案也保留。候选可为多菜组合，优先覆盖更多确认食材，并如实列出没用到的食材。单候选可直接说“好”或“开始吧”确认，多候选按编号或菜名选择。

照片表示本次想用的食材，不代表全部厨房库存。默认常备调料由 `pantry_defaults.py` 统一定义；明确缺少的配料和忌口优先排除。常备调料不算入确认食材覆盖率，特殊配料不默认已有。

### 局域网控制台与图片推荐

控制台每 0.5 秒尝试读取同一进程中的厨房状态；上次读取未完成时复用进行中的请求，避免轮询重叠。页面展示机器人模拟反馈、摄像头、菜谱、步骤与计时；页面展示最新反馈事件，执行阶段与菜谱步骤号分别展示。照片可来自手机拍摄、文件上传或本机摄像头单帧；图片只在内存中处理，不落盘，单张限制 8 MB。

多模态模型返回“可见食材”和“不确定物品”两组结果。用户可以删除错误项或手动补充，只有点击“生成菜谱方案”后，确认食材才会写入厨房会话并触发菜谱推荐。控制台没有火源、刀具或高温设备的远程控制接口。

控制台顶部提供本地菜谱搜索入口，可按菜名或食材筛选。点击本地菜谱、推荐候选或当前“做菜进度”卡片，会打开 iOS 风格菜谱 Sheet，展示完整食材、用量与全部步骤；选择人数后可直接选择菜谱并进入做菜流程。

### 视频与图文教程导入

控制台的“把教程变成可执行菜谱”位于“拍下食材”上方，支持粘贴小红书视频或图文分享文案，自动识别类型；也可上传 MP4/MOV。视频限制 3 分钟、100 MB；后台处理不会改变当前厨房任务。公开移动分享页可以读取时直接获取视频，遇到登录、验证码或不可用内容时提供上传兜底。可选配置 `VIDEO_IMPORT_API_KEY` 接入 Wellbyte；该渠道需要自行具备可用账号及额度。

若桌面网络将小红书域名解析到 `198.18.0.0/15` 的代理占位地址（包括 IPv4-mapped / IPv4-translated IPv6 表示），运行时只对受支持的小红书及 `xhscdn.com` 域名查询固定可信 HTTPS DNS，并将请求固定到验证后的公网 IP，同时保留原域名的 Host 和 TLS SNI；其他域名或私网解析仍会被拒绝。分享页暂时返回不含教程数据的落地页时，最多重新读取一次，不重复 AI 推理；错误保留实际原因。图文帖子从公开页读取正文和按原帖顺序排列的图片，不转交视频下载通道。

视频理解复用 `DASHSCOPE_API_KEY` 和 `QWEN_BASE_URL`，使用 `QWEN_VIDEO_MODEL`（默认 `qwen3-omni-flash`），独立请求超时由 `QWEN_VIDEO_TIMEOUT_SECONDS` 配置（默认 90 秒）。基础依赖中的 `imageio-ffmpeg` 提供 FFmpeg；也可使用系统安装的媒体工具。较长视频分段理解后合并，视频的剪辑时间不直接作为烹饪时间。

图文识别使用已配置的视觉模型 `QWEN_VISION_MODEL`（默认 `qwen3-vl-flash`）。支持最多 16 张 JPEG/PNG/WebP 图片，单张 8 MB / 2400 万像素以内，三路并行下载但保留原顺序；最长边缩至 1600 像素，准备后总量限制 16 MB。通过受限公网下载读取图片，正文和全部图片一次发送给模型直接整理菜谱；纯正文帖子使用文本整理，不做视频压缩或分段。无需为每张图单独 OCR 或重复整理。图文分析启用模型 JSON Mode，避免分数等用量破坏结构化结果（[千问结构化输出说明](https://help.aliyun.com/zh/model-studio/qwen-structured-output)）。同一次响应中摘录教程原文并生成菜谱；证据必须引用对应图片摘录或正文的原句，计时证据核对秒数。图文证据保留图片序号，未能证实的用量和计时标为 AI 建议；重复或冲突的模型来源标记按 AI 建议处理；准备步骤拆分后重新对应证据与 AI 字段，已发生的“炖煮多久后”不重复启动倒计时。生成用量已有“适量”“少许”“足量”等文字时允许无单位确认保存。签名图片地址与图像负载仅在任务处理期间保留，不写入草稿、状态或保存菜谱。

以下带日期的性能记录来自既有历史验证，本轮代码同步未复测，也不代表当前环境的性能保证。

2026-09-17 使用图文分享链接 `https://xhslink.cn/o/36kL5Eir0ND` 的最终单次实测：读取 8 张图片并生成“咖喱沙茶牛排骨”，15 项食材、11 个步骤，通过隔离临时目录中的确认保存检查，未启动教学。总计 28.3 秒（读取 7.8、模型 20.5、整理 0.03）；11.9 秒出现首项内容、16.6 秒出现首步；一次模型请求，输入 12,870 / 输出 2,614 Token。无需额外文字补全请求。已验证“足量”无单位、分数用量、来源字段错位、准备步骤拆分及中途观察不重复计时。该结果是单次样本，实际耗时与公开页面可用性、网络和模型输出有关。

120 秒以内且满足模型负载限制的视频可走单段完整菜谱理解；多段视频的最终合并只发送文字证据，不重复上传视频。必要的缺失信息补全仍可能追加文字请求。流式生成的食材和步骤作为只读预览显示，完整结果仍须校验及用户确认。状态接口的 `metrics` 记录阶段耗时、压缩和模型耗时、首项内容和首步出现时间、请求次数与实际返回的 Token 用量；完成后的耗时不随核对时间增长，未知用量保持空值。

2026-09-17 在本机使用 50 秒“三杯鸡”链接的单次实测：读取 29.4 秒、压缩 20.6 秒、模型 37.2 秒，总计 87.3 秒；66.0 秒出现首项食材，76.0 秒出现首个步骤。一次模型请求使用输入 15,259 / 输出 1,401 Token。这是单个样本，不能作为所有平台和网络条件的延迟保证。后续可评估字幕优先、音频转写与关键帧抽取并行，再一次整理菜谱；尚未启用该路径。

计时一致性修复后，同一链接复测为 74.5 秒（读取 21.3、压缩 19.6、模型 33.6）；52.4 秒出现首项食材、60.0 秒出现首步，输入 15,259 / 输出 2,015 Token。两次均只调用一次模型；耗时差异包含网络与生成波动，不能归因于本地计时修复。这两次测量早于自动 AI 补全调整。

IPv6 代理占位地址识别修复后，柠檬鸡扒、三杯鸡和此前的牛肉分享链接均能读取视频来源；柠檬鸡扒完整导入实测 53.5 秒（读取 7.0、压缩 10.3、模型 36.2），22.3 秒出现首项内容、28.5 秒出现首步，一次模型请求使用输入 17,375 / 输出 2,529 Token，最终 9 项食材、9 个步骤在隔离临时目录中通过确认保存检查。未启动教学。这是单次样本，不代表所有分享链接或网络条件的成功率与耗时保证。

视频未说明的用量、必要做法和烹饪时间由 AI 给出合理建议并标记来源；明确的视频信息和用户修改优先保留。主要肉类尽量提供具体克数或个数，缺少明确量时按人数给 AI 建议；调料等已有“适量”“少量”“按口味”“一圈”表述允许确认。优先在原次视频分析中补全，仅在仍缺少信息时追加一次精简的文字请求，只返回缺失项，不重新上传视频。已有不完整草稿可使用“AI 补全缺失信息”，完成后仍须用户核对和确认；旧的缺少信息提示不会在问题已解决后继续拦截。切配、装盘等无需计时的动作可保持计时空值。

控制台默认一人份，打开详情与选择菜谱沿用当前人数，详情内改人数时按该人数重载用量；已明确的视频份量保留原值。食材显示不会重复拼接已有单位，定性用量不附加多余单位，例如“两勺”“半碗”“适量”。

结果先作为可编辑草稿展示，区分视频/图文提取、AI 补全、用户修改和本地规则调整。修改后先保存并核对整理后的版本，再确认保存到“已导入菜谱”；确认不会再次改变步骤或计时。菜谱保存在 `skills/kitchen_assistant/recipes/imported/`，不提交到 Git；未确认草稿仅在本进程中保留，临时媒体在处理收尾阶段删除。

已导入菜谱复用完整菜谱 Sheet 与机器人教学。存在活动任务时须先结束，再开始另一份导入菜谱；计时由用户明确启动。控制台进入带时长步骤时，主按钮先显示“开始计时”；启动后再点“下一步”会直接切换并清理旧步骤计时，不再二次确认。自然语言提前推进仍有计时确认流程，详见下文。烹饪 API 还支持完成确认、暂停恢复、食材状态确认及结束任务；页面主按钮按当前状态确认食材或恢复，独立操作保留结束任务。YouTube 链接首版暂不接入，可上传相应视频文件。

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

AI 在同一响应中返回候选及完整详情；单道菜提示要求 6–10 步，多菜组合保留完整做法。候选逐条校验，部分有效时保留有效结果；响应后半截被截断时，只接收前面完整闭合且通过校验的对象。没有有效结果时，符合条件的错误最多定向纠错一次；剩余预算不足 2 秒不纠错，纠错超时最多 8 秒且不超过剩余预算。失败后按请求约束尝试本地回退，明确绕过缓存时不回退。

有效详情保存在内存与 `skills/kitchen_assistant/recipes/generated/`，确认候选无需再次请求模型补详情。Prompt 与本地校验共享 `recipe_contract.py` 的约束；生成 JSON 是运行数据，默认被 Git 忽略。

### 本地菜谱目录

本地固定目录共有 440 道菜，统一存放在 `recipes/catalog/`：肉类、蔬菜、水产、主食与汤、轻食与减脂。其中 90 道参考 HowToCook 固定提交 `c05758fa661ac4efa0361a987b700a351a22159b`，并非额外增加 90 道。`recipes/recipes.json` 的 `recipes` 数组为空，仅保留菜品流程配置。HowToCook 参考菜谱按 `sources.json` 记录的 Unlicense 使用，保留来源链接、提交版本和许可证；步骤经过本地校订，运行时不抓取或更新上游内容。

修改目录后可执行离线审计：

```bash
python scripts/validate_recipe_catalog.py
```

审计检查固定目录的来源版本、许可证、重复 ID/菜名、模糊用量及 1、2、3 人份标准化，不等同于检查所有个人导入菜谱。完整来源和转换原则见 `skills/kitchen_assistant/recipes/sources.json`。

清理与本地目录同名的 AI 生成缓存时，先预览再执行：

```bash
python scripts/prune_generated_recipe_duplicates.py
python scripts/prune_generated_recipe_duplicates.py --apply
```

### 计时与并行准备规则

- 只有加热、预热、煮炖焖煎烤蒸炸焯炒收汁，以及腌制、浸泡、泡发、静置等步骤保留计时。
- 控制台选择或导航进入带时长步骤时不自动开始计时，主按钮先显示“开始计时”；启动后再点“下一步”会直接导航，离开步骤时清理原计时。未确认食材、暂停、取消或完成时不能直接导航。
- 自然语言“下一步”“跳过”或完成语句遇到尚未启动的计时步骤，会要求选择开始计时或“确认完成”；运行中提前推进则要求再次确认。控制台的导航接口不使用这套二次确认规则。
- 普通文本流程支持“开始”“下锅了”“开始计时”等指令。当前代码在非导入、非控制台菜谱的烹饪起始步骤，以及特定菜品的确认后续步骤，存在由本地流程启动计时的分支，不能概括为所有入口都必须单独说“开始计时”。
- 普通计时到期提醒检查实际状态，不判断熟度；特定菜品可通过 `timer_end_action` 配置等待确认或推进步骤。计时结果不构成食品安全判断。
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

- 已有语音输入和播报代码；完整麦克风、真实 ASR、播放和连续对话体验仍待目标设备验证。
- 单 Python 进程、单活动会话，没有用户身份、跨进程恢复或持久化会话。
- 视觉能力只盘点本机摄像头或上传图片中的可见食材，不能确认过敏原、食品安全或食物是否已熟。相机权限和平台兼容性需单独验证。
- 未接通真实网页菜谱搜索。
- AI 内容存在不确定性，图片食材必须由用户确认，菜谱结果仍由本地 Schema 和标准化规则约束。

## 下一阶段计划

1. 增加会话持久化和多个独立计时器。
2. 接入真实机器人 SDK，并联调机器人侧摄像头和状态反馈。
3. 在获得官方搜索 API、鉴权和响应样例后实现带来源引用的真实菜谱搜索。
4. 增加缓存版本、过期策略和运行数据清理命令。



更细的厨房 Skill 输入输出和限制见 [skills/kitchen_assistant/SKILL.md](../skills/kitchen_assistant/SKILL.md)，模块边界见 [docs/ARCHITECTURE.md](../docs/ARCHITECTURE.md)。
