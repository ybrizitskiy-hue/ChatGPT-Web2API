from chatgpt_web2api.opencode_bridge_session import (
    SessionOpenCodeBridge,
    _CONTINUATION_MARKER,
)


def _tool(name: str) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": f"OpenCode {name} tool",
            "parameters": {
                "type": "object",
                "properties": {"value": {"type": "string"}},
            },
        },
    }


def test_same_chat_user_followup_refreshes_current_tool_protocol():
    body = {
        "conversation_id": "conv-1",
        _CONTINUATION_MARKER: True,
        "messages": [{"role": "user", "content": "Inspect the repository."}],
        "tools": [_tool("bash"), _tool("webfetch")],
        "tool_choice": "auto",
        "stream": True,
    }

    upstream = SessionOpenCodeBridge._prepare_upstream_body(body)

    assert upstream["conversation_id"] == "conv-1"
    assert upstream["stream"] is False
    assert "tools" not in upstream
    assert "tool_choice" not in upstream
    assert _CONTINUATION_MARKER not in upstream
    assert upstream["messages"][0]["role"] == "system"
    protocol = upstream["messages"][0]["content"]
    assert "[OpenCode tool protocol]" in protocol
    assert '"name":"bash"' in protocol
    assert '"name":"webfetch"' in protocol
    assert "ChatGPT web page is transport only" in protocol
    assert upstream["messages"][1] == {"role": "user", "content": "Inspect the repository."}


def test_same_chat_required_followup_makes_required_policy_visible_to_model():
    body = {
        "conversation_id": "conv-1",
        _CONTINUATION_MARKER: True,
        "messages": [{"role": "user", "content": "Inspect this GitHub repository."}],
        "tools": [_tool("bash"), _tool("webfetch")],
        "tool_choice": "required",
    }

    upstream = SessionOpenCodeBridge._prepare_upstream_body(body)
    protocol = upstream["messages"][0]["content"]

    assert "You MUST request exactly one of the available tools in this response." in protocol
    assert "NEVER invoke ChatGPT-native web browsing" in protocol
    assert "GitHub authorization flows" in protocol


def test_tool_result_continuation_stays_delta_only_without_schema_replay():
    body = {
        "conversation_id": "conv-1",
        _CONTINUATION_MARKER: True,
        "messages": [
            {
                "role": "tool",
                "tool_call_id": "call-1",
                "content": "tool-output",
            }
        ],
        "tools": [_tool("bash"), _tool("read")],
        "tool_choice": "auto",
    }

    upstream = SessionOpenCodeBridge._prepare_upstream_body(body)

    assert upstream["messages"] == [
        {"role": "user", "content": "[Tool result call-1]\ntool-output"}
    ]


def test_github_policy_becomes_visible_on_same_chat_followup():
    original = {
        "conversation_id": "conv-1",
        _CONTINUATION_MARKER: True,
        "messages": [
            {
                "role": "user",
                "content": "Inspect this GitHub repository and tell me the latest commit.",
            }
        ],
        "tools": [_tool("bash"), _tool("webfetch")],
        "tool_choice": "auto",
    }

    forced = SessionOpenCodeBridge._force_opencode_tool_for_github(original)
    assert forced["tool_choice"] == "required"

    upstream = SessionOpenCodeBridge._prepare_upstream_body(forced)
    protocol = upstream["messages"][0]["content"]
    assert "You MUST request exactly one of the available tools in this response." in protocol
    assert '"name":"bash"' in protocol
    assert '"name":"webfetch"' in protocol


def test_tool_choice_none_does_not_refresh_tool_protocol():
    body = {
        "conversation_id": "conv-1",
        _CONTINUATION_MARKER: True,
        "messages": [{"role": "user", "content": "Explain the previous result."}],
        "tools": [_tool("read")],
        "tool_choice": "none",
    }

    upstream = SessionOpenCodeBridge._prepare_upstream_body(body)

    assert upstream["messages"] == [{"role": "user", "content": "Explain the previous result."}]
