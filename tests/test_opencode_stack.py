import json
from pathlib import Path

import pytest

from chatgpt_web2api.opencode_stack import bridge_root, is_local_url, load_state


def test_bridge_root_removes_v1_path():
    assert bridge_root("http://127.0.0.1:8010/v1") == "http://127.0.0.1:8010"


def test_bridge_root_rejects_invalid_url():
    with pytest.raises(RuntimeError):
        bridge_root("localhost:8010/v1")


def test_local_url_detection():
    assert is_local_url("http://127.0.0.1:8000")
    assert is_local_url("http://localhost:8000")
    assert is_local_url("http://[::1]:8000")
    assert not is_local_url("https://bridge.example.com")


def test_load_state_validates_required_fields(tmp_path: Path):
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps(
            {
                "upstream_url": "http://127.0.0.1:8000",
                "bridge_url": "http://127.0.0.1:8010/v1",
            }
        ),
        encoding="utf-8",
    )
    assert load_state(path)["bridge_url"].endswith("/v1")

    path.write_text("{}", encoding="utf-8")
    with pytest.raises(RuntimeError, match="missing upstream_url"):
        load_state(path)
