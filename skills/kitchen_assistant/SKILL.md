# AI 厨房助手 Skill

这个单进程 Skill 支持指定菜名搜索或按已有食材推荐。默认 `MockRecipeSearchProvider` 提供本地示例菜谱；若 `ARK_API_KEY` 已设置，则 `DoubaoAIRecipeProvider` 用普通 Chat Completions 生成候选和结构化菜谱。用户先选择候选并明确确认，才会进入逐步烹饪。

AI 生成的最多三个候选会分别生成并持久化完整菜谱到 `recipes/generated/`。后续同菜名、人数、口味和忌口请求优先使用本地缓存，不再次调用生成模型。`RecipeNormalizer` 会拆分过密的备菜步骤，并修正明显过短的肉类和蔬菜炒制计时；计时始终是下限参考，实际完成状态以可观察的熟度为准。

`recipes/recipes.json` 集中保存本地回退菜谱和离线候选。厨房反馈必须使用老师提供的 Mock SDK 已有动作和灯带值；这些值只用于本地模拟，不是任何真实机器人厂商的动作 ID。

`llm/doubao_client.py` 只从 `ARK_API_KEY` 读取 Key，支持 `DOUBAO_BASE_URL`、`DOUBAO_MODEL` 覆盖默认配置；异常或无 Key 会回退本地 Mock。`providers/online_recipe_provider.py` 的 `web_search` 仍是禁用占位，绝不会抓取随机网页；未来接入前仍需要官方搜索 API 文档、Base URL、鉴权和请求/响应样例。
