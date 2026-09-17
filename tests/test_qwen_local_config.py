from llm import config


def test_local_config_corrects_inherited_credentials_without_executing_values(monkeypatch, tmp_path):
    path = tmp_path / "qwen.env"
    path.write_text("DASHSCOPE_API_KEY='local-test-key'\nQWEN_BASE_URL=https://example.invalid/v1\nHOME=$(touch marker)\nQWEN_TIMEOUT_SECONDS=12\n", encoding="utf-8")
    monkeypatch.setattr(config, "LOCAL_CONFIG_PATH", path)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "inherited-test-key")
    settings = config.QwenConfig.from_environment()
    assert settings.api_key == "local-test-key"
    assert settings.base_url == "https://example.invalid/v1"
    assert settings.timeout == 12
    assert not (tmp_path / "marker").exists()


def test_empty_local_values_preserve_explicit_environment_settings(monkeypatch, tmp_path):
    path = tmp_path / "qwen.env"
    path.write_text("DASHSCOPE_API_KEY=\nQWEN_BASE_URL=\n", encoding="utf-8")
    monkeypatch.setattr(config, "LOCAL_CONFIG_PATH", path)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "environment-test-key")
    monkeypatch.setenv("QWEN_BASE_URL", "https://example.invalid/v1")
    settings = config.QwenConfig.from_environment()
    assert settings.api_key == "environment-test-key"
    assert settings.base_url == "https://example.invalid/v1"


def test_windows_notepad_utf8_bom_preserves_first_api_key(monkeypatch, tmp_path):
    path = tmp_path / "qwen.env"
    path.write_text("DASHSCOPE_API_KEY=notepad-test-key\r\n", encoding="utf-8-sig")
    monkeypatch.setattr(config, "LOCAL_CONFIG_PATH", path)
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    assert config.QwenConfig.from_environment().api_key == "notepad-test-key"
