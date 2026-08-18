from chatgpt_web2api.opencode_bridge import _UPSTREAM
from chatgpt_web2api.opencode_bridge_runtime import DEFAULT_UPSTREAM
from chatgpt_web2api.opencode_setup_common import DEFAULT_UPSTREAM as SETUP_UPSTREAM


def test_all_opencode_upstream_defaults_use_web2api_8080():
    assert _UPSTREAM == "http://127.0.0.1:8080"
    assert DEFAULT_UPSTREAM == _UPSTREAM
    assert SETUP_UPSTREAM == _UPSTREAM
