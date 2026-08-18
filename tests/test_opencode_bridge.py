import json

import pytest

from chatgpt_web2api.opencode_bridge import OpenCodeBridge, ToolProtocolError


def _tool(name="read"):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": f"Tool {name}",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
            },
        },
    }


def _payload(content):
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 1,
        "model": "gpt-5",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def test_fingerprint_ignores_stream_transport_mode_and_stream_options():
    base = {"model": "gpt-5", "messages": [{"role": "user", "content": "hello"}]}
    streaming = {**base, "stream": True, "stream_options": {"include_usage": True}}
    nonstreaming = {**base, "stream": False}
    assert OpenCodeBridge._fingerprint(streaming, "Bearer x") == OpenCodeBridge._fingerprint(
        nonstreaming, "Bearer x"
    )


def test_fingerprint_isolates_authorization_context():
    body = {"model": "gpt-5", "messages": [{"role": "user", "content": "hello"}]}
    assert OpenCodeBridge._fingerprint(body, "Bearer a") != OpenCodeBridge._fingerprint(
        body, "Bearer b"
    )


def test_prepare_upstream_body_injects_tools_and_removes_openai_tool_fields():
    body = {
        "model": "gpt-5",
        "stream": True,
        "stream_options": {"include_usage": True},
        "messages": [{"role": "user", "content": "read the file"}],
        "tools": [_tool()],
        "tool_choice": "auto",
        "parallel_tool_calls": False,
    }
    result = OpenCodeBridge._prepare_upstream_body(body)
    assert result["stream"] is False
    assert "stream_options" not in result
    assert "tools" not in result
    assert "tool_choice" not in result
    assert "parallel_tool_calls" not in result
    assert result["messages"][0]["role"] == "system"
    assert "__W2A_TOOL_CALL__" in result["messages"][0]["content"]
    assert '"name":"read"' in result["messages"][0]["content"]


def test_tool_choice_none_does_not_inject_protocol():
    body = {
        "model": "gpt-5",
        "messages": [{"role": "user", "content": "answer only"}],
        "tools": [_tool()],
        "tool_choice": "none",
    }
    result = OpenCodeBridge._prepare_upstream_body(body)
    assert result["messages"] == body["messages"]


def test_required_and_named_tool_choice_are_expressed_in_prompt():
    required = {
        "messages": [{"role": "user", "content": "x"}],
        "tools": [_tool("read"), _tool("bash")],
        "tool_choice": "required",
    }
    text = OpenCodeBridge._prepare_upstream_body(required)["messages"][0]["content"]
    assert "MUST request exactly one" in text

    named = {
        **required,
        "tool_choice": {"type": "function", "function": {"name": "bash"}},
    }
    text = OpenCodeBridge._prepare_upstream_body(named)["messages"][0]["content"]
    assert 'tool named "bash"' in text


def test_invalid_named_tool_choice_rejected_before_upstream():
    body = {
        "messages": [{"role": "user", "content": "x"}],
        "tools": [_tool("read")],
        "tool_choice": {"type": "function", "function": {"name": "missing"}},
    }
    with pytest.raises(ToolProtocolError):
        OpenCodeBridge._prepare_upstream_body(body)


def test_normalise_messages_preserves_tool_result_as_visible_context():
    body = {
        "messages": [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_abc",
                        "type": "function",
                        "function": {"name": "read", "arguments": '{"path":"README.md"}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_abc", "content": "hello world"},
        ]
    }
    messages = OpenCodeBridge._normalise_messages(body)
    assert messages[0]["role"] == "assistant"
    assert "read" in messages[0]["content"]
    assert "tool_calls" not in messages[0]
    assert messages[1] == {
        "role": "user",
        "content": "[Tool result call_abc]\nhello world",
    }


def test_translate_completion_returns_openai_tool_call_shape():
    body = {"tools": [_tool("read")], "tool_choice": "auto"}
    payload = _payload(
        '{"__W2A_TOOL_CALL__":true,"name":"read","arguments":{"path":"README.md"}}'
    )
    translated = OpenCodeBridge._translate_completion(payload, body)
    choice = translated["choices"][0]
    call = choice["message"]["tool_calls"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert choice["message"]["content"] is None
    assert call["type"] == "function"
    assert call["function"]["name"] == "read"
    assert json.loads(call["function"]["arguments"]) == {"path": "README.md"}


def test_translate_completion_rejects_unknown_tool_name():
    body = {"tools": [_tool("read")], "tool_choice": "auto"}
    payload = _payload('{"__W2A_TOOL_CALL__":true,"name":"bash","arguments":{}}')
    with pytest.raises(ToolProtocolError) as exc:
        OpenCodeBridge._translate_completion(payload, body)
    assert exc.value.code == "unknown_tool_call"


def test_required_tool_choice_rejects_plain_text_completion():
    body = {"tools": [_tool("read")], "tool_choice": "required"}
    with pytest.raises(ToolProtocolError) as exc:
        OpenCodeBridge._translate_completion(_payload("plain text"), body)
    assert exc.value.code == "tool_call_required"


def test_plain_text_completion_is_unchanged_for_auto():
    payload = _payload("No tool needed.")
    body = {"tools": [_tool("read")], "tool_choice": "auto"}
    assert OpenCodeBridge._translate_completion(payload, body) == payload


def test_parser_accepts_json_fence_and_string_encoded_arguments():
    fenced = "```json\n{\"__W2A_TOOL_CALL__\":true,\"name\":\"bash\",\"arguments\":{\"cmd\":\"pwd\"}}\n```"
    assert OpenCodeBridge._parse_tool_call(fenced) == ("bash", {"cmd": "pwd"})

    encoded = '{"__W2A_TOOL_CALL__":true,"name":"bash","arguments":"{\\"cmd\\":\\"pwd\\"}"}'
    assert OpenCodeBridge._parse_tool_call(encoded) == ("bash", {"cmd": "pwd"})


def test_parser_does_not_accept_arbitrary_json():
    assert OpenCodeBridge._parse_tool_call('{"name":"bash","arguments":{}}') is None


def test_sse_tool_call_has_index_and_separate_finish_event():
    body = {"tools": [_tool("read")], "tool_choice": "auto"}
    payload = OpenCodeBridge._translate_completion(
        _payload('{"__W2A_TOOL_CALL__":true,"name":"read","arguments":{"path":"README.md"}}'),
        body,
    )
    raw = OpenCodeBridge._as_sse(payload).decode()
    events = [
        json.loads(line[6:])
        for line in raw.splitlines()
        if line.startswith("data: {")
    ]
    first = events[0]["choices"][0]
    tool_delta = first["delta"]["tool_calls"][0]
    assert tool_delta["index"] == 0
    assert tool_delta["function"]["name"] == "read"
    assert first["finish_reason"] is None
    assert events[1]["choices"][0]["finish_reason"] == "tool_calls"
    assert events[1]["choices"][0]["delta"] == {}


def test_sse_plain_text_uses_text_then_stop_boundary():
    raw = OpenCodeBridge._as_sse(_payload("hello")).decode()
    events = [
        json.loads(line[6:])
        for line in raw.splitlines()
        if line.startswith("data: {")
    ]
    assert events[0]["choices"][0]["delta"] == {"content": "hello"}
    assert events[0]["choices"][0]["finish_reason"] is None
    assert events[1]["choices"][0]["finish_reason"] == "stop"
