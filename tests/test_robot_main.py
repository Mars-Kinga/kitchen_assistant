from robot_main import is_exit_command


def test_exit_command_accepts_common_typed_and_asr_variants() -> None:
    for text in ("再见", "拜拜", "我先走了", "退出程序", "Goodbye", " bye！"):
        assert is_exit_command(text)
    assert not is_exit_command("取消计时")
