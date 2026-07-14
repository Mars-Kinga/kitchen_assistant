# 尚缺的真实接入资料

## 真实菜谱搜索

- 选定的 API 或搜索服务及官方文档
- Base URL、请求参数、鉴权方式、响应样例、错误码和调用限制
- 是否允许展示、缓存和再分发菜谱内容，以及来源链接要求
- 超时、重试、分页、空结果与非法 JSON 的处理要求

未提供前，`OnlineRecipeSearchProvider` 不会联网，默认使用 `MockRecipeSearchProvider`。

## 已确认的豆包 Chat Completions

本项目已按确认信息接入普通 Chat Completions：Python SDK 为 `openai`，Base URL 为 `https://ark.cn-beijing.volces.com/api/v3`，默认模型为 `doubao-seed-2-0-mini-260428`，Key 只从 `ARK_API_KEY` 环境变量读取。可选覆盖变量是 `DOUBAO_BASE_URL` 与 `DOUBAO_MODEL`。

这项能力只用于 AI 生成候选菜谱、结构化菜谱和规则未覆盖的普通问答；它不是网页搜索，也不自动拥有联网、浏览、Managed Agent 或工具调用能力。Key 不得提交或发送到聊天；项目不会自动读取 `.env` 文件。

## 豆包联网搜索的已知入口与仍缺资料

火山方舟官方文档目录已列出 Responses API、Web Search、Function Calling 和 Managed Agents。因此更合适的后续路线是：保留当前 Chat Completions 生成能力，并在 `OnlineRecipeSearchProvider` 内新增独立的 Responses Web Search 适配器，而不是让状态机抓网页。

官方入口：

- [火山方舟文档目录](https://www.volcengine.com/docs/82379/?lang=zh)
- [Web Search](https://docs.volcengine.com/docs/82379/1756990?lang=zh)
- [Responses API 工具调用](https://docs.volcengine.com/docs/82379/1958524?lang=zh)

真正实施前仍需在用户自己的火山方舟控制台确认并提供：

- 当前账号/地域是否已开通 Web Search 组件，以及是否需要单独计费或授权
- `doubao-seed-2-0-mini-260428` 是否支持 Responses API 的 Web Search；若不支持，允许使用的模型或 Endpoint ID
- 控制台官方示例的一份脱敏请求与响应，尤其是工具名称、引用 URL、标题和摘要字段
- 搜索条数、超时、限流、内容过滤、来源展示和缓存期限要求
- 是否接受把“AI 生成”和“真实搜索”作为两个可切换模式；建议默认仍为当前 `ai_generated`/`mock`

这些信息确认前，`web_search` 仍是占位模式，项目不会把 Chat Completions 生成内容标成网络结果。

## 智元真实机器人

- 具体机器人型号与 Skill 官方规范
- SDK 安装方法、语音/屏幕/动作/灯带/表情接口
- 可用动作和效果 ID、参数约束、安全边界与测试设备要求

未提供前，`MockRobotSDK` 仅输出控制台日志。当前厨房层严格使用老师提供的 Mock SDK 中已有动作与灯带值；它们绝非真实机器人接口。
