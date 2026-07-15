# AI 厨房助手 Skill

这个单进程 Skill 支持指定菜名搜索或按已有食材推荐。默认 `MockRecipeSearchProvider` 提供本地示例菜谱；若 `ARK_API_KEY` 已设置，则 `DoubaoAIRecipeProvider` 用普通 Chat Completions 生成候选和结构化菜谱。用户先选择候选并明确确认，才会进入逐步烹饪。

AI 生成的最多三个候选会并行生成，并合并持久化到一个按菜名命名的 JSON 文件（如 `recipes/generated/cached_宫保鸡丁.json`）。后续同菜名、口味和忌口请求优先使用本地缓存；人数不同也会复用同一份基准菜谱，由 `RecipeNormalizer` 同步缩放食材清单和步骤中的用量，不再次调用生成模型。程序启动时会清理旧版哈希命名的单菜谱缓存。`RecipeNormalizer` 会拆分过密的备菜步骤、补齐漏填的肉类炒制下限时间和腌制/浸泡等明确时长，并保留屏幕上的完整指令；计时始终是下限参考，时间到后先检查实际熟度，不会自动跳到下一步。等待腌制时会提示用户准备下一步调料或蔬菜，用户可以用“开始了”“给我计时”等自然表达启动计时。

`recipes/recipes.json` 集中保存本地回退菜谱和离线候选。厨房反馈必须使用老师提供的 Mock SDK 已有动作和灯带值；这些值只用于本地模拟，不是任何真实机器人厂商的动作 ID。

`llm/doubao_client.py` 只从 `ARK_API_KEY` 读取 Key，支持 `DOUBAO_BASE_URL`、`DOUBAO_MODEL` 覆盖默认配置；异常或无 Key 会回退本地 Mock。`providers/online_recipe_provider.py` 的 `web_search` 仍是禁用占位，绝不会抓取随机网页；未来接入前仍需要官方搜索 API 文档、Base URL、鉴权和请求/响应样例。
