from pathlib import Path

from chatgpt_web2api.opencode_baseline import (
    DIRECT_PROVIDER,
    configure_direct_provider,
    restore_stock_core_tuning,
)
from chatgpt_web2api.opencode_setup_common import configure_core, load_object


def test_package_startup_does_not_install_browser_monkey_patches():
    package_init = Path(__file__).parents[1] / "src" / "chatgpt_web2api" / "__init__.py"
    source = package_init.read_text(encoding="utf-8")
    assert "runtime_hotfixes" not in source
    assert "input_recovery" not in source


def test_setup_restores_stock_detector_tuning(tmp_path):
    config_path = tmp_path / "config.json"
    configure_core(
        config_path,
        "local-secret",
        "http://127.0.0.1:8080",
        "gpt-5.6-sol-wm",
    )

    before = load_object(config_path)
    assert before["request_timeout"] == 930
    assert before["detector_reasoning_stream_idle_timeout_seconds"] == 20.0
    assert before["detector_default_stream_idle_timeout_seconds"] == 20.0

    assert restore_stock_core_tuning(config_path) is True
    after = load_object(config_path)

    assert after["host"] == "127.0.0.1"
    assert after["port"] == 8080
    assert "local-secret" in after["api_keys"]
    assert after["default_model"] == "gpt-5.6-sol-wm"
    for key in (
        "request_timeout",
        "detector_hard_timeout_seconds",
        "detector_reasoning_first_content_timeout_seconds",
        "detector_default_first_content_timeout_seconds",
        "detector_reasoning_stream_idle_timeout_seconds",
        "detector_default_stream_idle_timeout_seconds",
    ):
        assert key not in after


def test_cleanup_preserves_user_custom_tuning(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        '{"request_timeout": 777, "detector_default_stream_idle_timeout_seconds": 33}',
        encoding="utf-8",
    )
    assert restore_stock_core_tuning(config_path) is False
    after = load_object(config_path)
    assert after["request_timeout"] == 777
    assert after["detector_default_stream_idle_timeout_seconds"] == 33


def test_direct_provider_bypasses_sidecar(tmp_path):
    config_path = tmp_path / "opencode.json"
    key_file = tmp_path / "key"
    key_file.write_text("secret\n", encoding="utf-8")

    configure_direct_provider(
        config_path,
        upstream="http://127.0.0.1:8080",
        key_file=key_file,
        model="gpt-5.6-sol-wm",
        model_name="ChatGPT Web Sol",
        set_default=True,
    )

    config = load_object(config_path)
    provider = config["provider"][DIRECT_PROVIDER]
    assert provider["options"]["baseURL"] == "http://127.0.0.1:8080/v1"
    assert provider["options"]["apiKey"].startswith("{file:")
    assert provider["models"]["gpt-5.6-sol-wm"]["tool_call"] is False
    assert config["model"] == f"{DIRECT_PROVIDER}/gpt-5.6-sol-wm"
