# 接入配置与待补资料

2026-11-03 按本次同步的仓库实现核对。代码与共享说明一起发布；真实账号、目标设备及平台体验仍需分别验证。

## 已有千问文字、视觉与教程理解

项目通过 `openai` Python SDK 使用 OpenAI 兼容接口。当前默认文字模型为 `qwen3-omni-flash`，视觉模型为 `qwen3-vl-flash`，请求关闭深度思考，文字生成使用流式输出。服务地址、模型名及权限须与实际账号匹配。

`skills/kitchen_assistant/llm/config.py` 先读环境变量，再用 `config/qwen.env` 中允许的非空值覆盖。允许项为：

- `DASHSCOPE_API_KEY`、`QWEN_BASE_URL`
- `QWEN_TEXT_MODEL`、`QWEN_VISION_MODEL`
- `QWEN_TIMEOUT_SECONDS`、`QWEN_VISION_TIMEOUT_SECONDS`、`QWEN_MAX_RETRIES`

不会自动读取根目录 `.env`，不会执行配置内容。`config/qwen.env` 被 Git 忽略；不要提交密钥，修改后重启服务。无千问配置时仍可使用本地固定菜谱和文字流程。

视频理解复用上述账号配置，视频模型与超时通过环境变量 `QWEN_VIDEO_MODEL`、`QWEN_VIDEO_TIMEOUT_SECONDS` 设置；这两个变量不在本地配置文件允许项列表内。图文理解使用视觉模型。第三方视频获取通道使用 `VIDEO_IMPORT_API_KEY` 和可选 `VIDEO_IMPORT_WELLBYTE_URL`，需独立账号与额度，它不等于千问权限。

待真实验证：账号是否允许目标模型、地址和地域是否匹配，以及文字、图片、视频/图文各自的请求结果。`scripts/check_qwen_connection.py` 会主动联网，只检查实际调用的路径，不替代所有功能验收。

## 真实菜谱搜索仍未接通

`OnlineRecipeSearchProvider` 仍为 `web_search` 占位，不发真实搜索请求。未配置 AI 时使用本地 Provider；配置 AI 后仍默认本地优先，未命中时生成。AI 生成和教程导入都不等于通用网页菜谱搜索。

实施搜索前需要：

- 选定服务及官方文档、地址、鉴权、请求/响应样例和错误码。
- 调用限制、超时、重试、分页及空结果规则。
- 来源链接、缓存、展示与再分发许可。
- 若使用百炼搜索组件，确认工作空间授权、模型工具协议和相关费用。
- 明确搜索与生成的模式选择及来源展示，继续区分 `web_search`、`ai_generated` 和本地结果。

## 真实机器人与相机

当前 `MockRobotSDK` 提供模拟动作、屏幕、灯带和表情，语音由 `VoiceOutput` 按配置播放或仅打印。控制台的 `simulated` 状态不是机器人遥测。本机相机已有 OpenCV 适配，目标平台权限和可用性仍需现场验证。

接入真实机器人仍需要：

- 具体型号、官方 Skill 规范与 SDK 安装方法。
- 五通道接口、动作/效果 ID、参数限制及故障反馈格式。
- 机器人侧摄像头读取和状态接口。
- 测试设备、允许操作范围与可观察的验收标准。

## 语音链路待验证

已有录音、VAD、ASR、TTS 与 `--voice` 入口。ASR 读取 `MIMO_API_KEY` 或 `TOKEN_PLAN_API_KEY`，可通过 `MIMO_BASE_URL`、`MIMO_ASR_MODEL` 配置，与千问独立。依赖见 `requirements-voice.txt`，本地千问配置文件不读取这些 ASR 项。

仍需确定目标电脑与麦克风、可用识别服务、播放后端，以及 `voice_outputs/` 录音留存和清理要求。安装成功、Mock 反馈或入口测试均不等于完整语音体验已验收。
