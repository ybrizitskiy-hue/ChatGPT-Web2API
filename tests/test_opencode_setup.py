import json
from pathlib import Path

from chatgpt_web2api.opencode_setup_common import (
    configure_core,
    configure_opencode,
    is_loopback,
    load_object,
    normalize_url,
    strip_jsonc,
    write_launchers,
)


def test_jsonc_parser_handles_comments_strings_and_trailing_commas(tmp_path):
    text = r'''
    {
      // comment
      "url": "https://example.test/a//b",
      "nested": {"value": 1, /* block */},
    }
    '''
    parsed = json.loads(strip_jsonc(text))
    assert parsed == {"url": "https://example.test/a//b", "nested": {"value": 1}}
    path = tmp_path / "opencode.jsonc"
    path.write_text(text, encoding="utf-8")
    assert load_object(path) == parsed


def test_url_normalisation_and_loopback():
    assert normalize_url("http://127.0.0.1:8010", v1=True) == "http://127.0.0.1:8010/v1"
    assert normalize_url("http://127.0.0.1:8080/v1", v1=False) == "http://127.0.0.1:8080"
    assert is_loopback("http://localhost:8080")
    assert not is_loopback("https://web2api.example.test")


def test_configure_core_preserves_settings_and_adds_reliability(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"log_level": "DEBUG", "api_keys": ["old"]}), encoding="utf-8")
    previous = configure_core(path, "new", "http://127.0.0.1:8080", "sol-model")
    config = json.loads(path.read_text(encoding="utf-8"))
    assert previous is not None and previous.exists()
    assert config["log_level"] == "DEBUG"
    assert config["api_keys"] == ["old", "new"]
    assert config["port"] == 8080
    assert config["request_timeout"] >= 930
    assert config["detector_hard_timeout_seconds"] >= 900
    assert config["default_model"] == "sol-model"


def test_configure_opencode_merges_models_options_and_uses_key_file(tmp_path):
    path = tmp_path / "opencode.json"
    path.write_text(
        json.dumps(
            {
                "provider": {
                    "chatgpt-web": {
                        "options": {"customOption": True},
                        "models": {"old-model": {"name": "Old model"}},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    key_file = tmp_path / "key file"
    key_file.write_text("secret\n", encoding="utf-8")
    previous = configure_opencode(
        path,
        provider_id="chatgpt-web",
        bridge_url="http://127.0.0.1:8010",
        key_file=key_file,
        model="sol-model",
        model_name="ChatGPT Sol",
        set_default=False,
        safe_permissions=True,
    )
    config = json.loads(path.read_text(encoding="utf-8"))
    assert previous is not None and previous.exists()
    provider = config["provider"]["chatgpt-web"]
    assert provider["npm"] == "@ai-sdk/openai-compatible"
    assert provider["options"]["baseURL"] == "http://127.0.0.1:8010/v1"
    assert provider["options"]["apiKey"].startswith("{file:")
    assert provider["options"]["timeout"] is False
    assert provider["options"]["headerTimeout"] == 960000
    assert provider["options"]["customOption"] is True
    assert provider["models"]["old-model"]["name"] == "Old model"
    assert provider["models"]["sol-model"]["tool_call"] is True
    assert provider["models"]["sol-model"]["reasoning"] is True
    assert config["permission"]["edit"] == "ask"
    assert config["model"] == "chatgpt-web/sol-model"


def test_existing_permission_policy_is_not_overwritten(tmp_path):
    path = tmp_path / "opencode.json"
    path.write_text(json.dumps({"permission": "allow"}), encoding="utf-8")
    key_file = tmp_path / "key"
    key_file.write_text("secret\n", encoding="utf-8")
    configure_opencode(
        path,
        provider_id="chatgpt-web",
        bridge_url="http://127.0.0.1:8010/v1",
        key_file=key_file,
        model="auto",
        model_name="ChatGPT Web",
        set_default=False,
        safe_permissions=True,
    )
    assert json.loads(path.read_text(encoding="utf-8"))["permission"] == "allow"


def test_launchers_call_setup_module_and_support_remote_services(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "chatgpt_web2api.opencode_setup_common.self_command",
        lambda: ["python", "-m", "chatgpt_web2api.opencode_setup"],
    )
    cmd, sh = write_launchers(
        tmp_path,
        config=Path("config.json"),
        upstream="https://web2api.example.test",
        bridge_url="https://bridge.example.test/v1",
        start_core=False,
        start_bridge=False,
    )
    for text in (cmd.read_text(encoding="utf-8"), sh.read_text(encoding="utf-8")):
        assert "chatgpt_web2api.opencode_setup" in text
        assert "--no-core" in text
        assert "--no-bridge" in text
