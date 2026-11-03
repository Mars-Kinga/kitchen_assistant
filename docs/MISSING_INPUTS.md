# 接入配置与待补资料（公开版本）

2026-11-03 按公开源码快照 `e645b53` 核对。当前 `main` 与较新的本地实现有区别，见 [版本范围](../README.md#published-version-and-local-development)。

## 已有千问适配与配置

已有 OpenAI 兼容客户端，用于菜谱生成、普通厨房问答和本机相机食材识别。默认文字模型 `qwen3-omni-flash`、视觉模型 `qwen3-vl-flash`，关闭深度思考；文字调用使用流式输出。这些默认值不保证账号具备相应权限。

**公开版本只读取环境变量**：`DASHSCOPE_API_KEY`、`QWEN_BASE_URL`、`QWEN_TEXT_MODEL`、`QWEN_VISION_MODEL`、`QWEN_TIMEOUT_SECONDS`、`QWEN_VISION_TIMEOUT_SECONDS`、`QWEN_MAX_RETRIES`。复制 `.env` 或 `config/qwen.env` 不会自动生效；需要在启动程序的终端设置变量，不能提交密钥。本地新版本的配置文件覆盖机制尚未发布到 `main`。

未配置千问时使用本地菜谱；配置后仍先查本地目录与已验证缓存。客户端默认零重试，但非法 JSON 或可纠正校验失败可以触发一次有预算限制的业务纠错。

真实运行仍需确认服务地址、地域、模型权限和可用凭据。`scripts/check_qwen_connection.py` 是显式联网检查，不属于默认离线测试，也不替代相机、语音或其他设备验证。

## 真实菜谱搜索仍未接通

`OnlineRecipeSearchProvider` 为占位，不发真实搜索请求。AI 生成不等于网络搜索。实现真实搜索前需要：

- 选定 API/服务及官方文档、Base URL、鉴权、参数、响应和错误码。
- 调用限制、超时、重试、分页、空结果与非法 JSON 规则。
- 来源展示、缓存和再分发许可。
- 如使用百炼搜索组件，确认工作空间授权、模型工具协议和费用。
- 明确搜索/生成模式与来源展示，保持 `web_search`、`ai_generated` 和本地模式区别。

## 真实机器人

当前机器人动作、屏幕、灯带和表情是 Mock；语音可由 TTS 播放。接入真实设备还需要具体型号、官方 Skill 规范、SDK 安装方法、五通道接口、可用动作/效果 ID、参数约束与故障反馈，以及允许操作范围和测试设备。

本机相机已有 OpenCV 单帧读取，不能将其描述为机器人侧摄像头已接通。公开版本也没有局域网控制台或设备遥测。

## 语音链路

录音、VAD、ASR 和 TTS 代码已存在，依赖在 `requirements-voice.txt`。ASR 使用 `MIMO_API_KEY` 或 `TOKEN_PLAN_API_KEY`，以及 `MIMO_BASE_URL`、`MIMO_ASR_MODEL`，与千问配置独立。

目标麦克风、识别服务、播放后端、连续对话和录音期间计时提醒仍需验证；`voice_outputs/` 的留存与清理也需确定。代码存在不代表真实设备体验已验收。

## 尚未公开的本地功能

本地继续开发的控制台、视频/图文教程导入及便携启动，不是安装一个缺少的依赖就能在当前 `main` 中开启的功能。其代码同步发布后，再按对应版本文档配置和验证。
