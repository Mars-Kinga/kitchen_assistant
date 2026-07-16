"""兼容文字入口。

所有路由、执行和参数解析统一复用 ``robot_main``，避免旧入口与主入口
形成两套实现。未来新增输入方式时也只需要复用同一 Runtime 主链路。
"""

from robot_main import main


if __name__ == "__main__":
    main()
