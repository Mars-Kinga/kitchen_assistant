import os
import sys

BASE_URL = os.getenv("DOUBAO_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3")
MODEL = os.getenv("DOUBAO_MODEL", "doubao-seed-2-0-mini-260428")


def main() -> int:
    api_key = os.getenv("ARK_API_KEY")
    if not api_key:
        print("错误：未设置 ARK_API_KEY 环境变量。", file=sys.stderr)
        return 1

    try:
        from openai import OpenAI
    except ImportError:
        print("错误：未安装 openai。请先运行 pip install -r requirements.txt。", file=sys.stderr)
        return 1

    client = OpenAI(
        base_url=BASE_URL,
        api_key=api_key,
        timeout=30.0,
        max_retries=2,
    )

    try:
        response = client.chat.completions.create(
            model=MODEL,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "你是一个谨慎、简洁的厨房助手。"
                        "优先给出适合做饭新手的安全、可执行建议。"
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        "我用小锅煮面，面条太长放不进去。"
                        "先把一端放进沸水，等它变软后再慢慢压进去，可以吗？"
                    ),
                },
            ],
            temperature=0.2,
        )
    except Exception as exc:
        # Provider error text can include request details; never echo it from a
        # diagnostic that users may paste into an issue.
        print(f"豆包调用失败：{type(exc).__name__}", file=sys.stderr)
        return 2

    if not response.choices:
        print("豆包返回了空结果。", file=sys.stderr)
        return 3

    content = response.choices[0].message.content
    if not content:
        print("豆包没有返回文本内容。", file=sys.stderr)
        return 4

    print(content)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
