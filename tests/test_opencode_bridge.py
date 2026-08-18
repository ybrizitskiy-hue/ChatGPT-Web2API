import json

from chatgpt_web2api.opencode_bridge import OpenCodeBridge


def test_fingerprint_ignores_stream_transport_mode():
    base = {"model": "gpt-5", "messages": [{"role": "user", "content": "hello"}]}
    streaming = {**base, "stream": True}
    nonstreaming = {**base, "stream": False}

    assert OpenCodeBridge._fingerprint(streaming) == OpenCodeBridge._fingerprint(nonstreaming)


def test_prepare_upstream_body_injects_tools_and_removes_openai_tool_fields():
    body = {
        "model": "gpt-5",
        "stream": True,
        "messages": [{"role": "user", "content": "read the file"}],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "read",
                    "description": "Read a file",
                    "parameters": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                        "required": ["path"],
                    },
                },
            }
        ],
        "tool_choice": "auto",
        "parallel_tool_calls": False,
    }

    result = OpenCodeBridge._prepare_upstream_body(body)

    assert result["stream"] is False
    assert "tools" not in result
    assert "tool_choice" not in result
    assert "parallel_tool_calls" not in result
    assert result["messages"][0]["role"] == "system"
    assert "__W2A_TOOL_CALL__" in result["messages"][0]["content"]
    assert '"name":"read"' in result["messages"][0]["content"]


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
    payload = {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 1,
        "model": "gpt-5",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": '{"__W2A_TOOL_CALL__":true,"name":"read","arguments":{"path":"README.md"}}',
                },
                "finish_reason": "stop",
            }
        ],
    }

    translated = OpenCodeBridge._translate_completion(payload)
    choice = translated["choices"][0]
    call = choice["message"]["tool_calls"][0]

    assert choice["finish_reason"] == "tool_calls"
    assert choice["message"]["content"] is None
    assert call["type"] == "function"
    assert call["function"]["name"] == "read"
    assert json.loads(call["function"]["arguments"]) == {"path": "README.md"}


def test_plain_text_completion_is_unchanged():
    payload = {
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "No tool needed."},
                "finish_reason": "stop",
            }
        ]
    }

    assert OpenCodeBridge._translate_completion(payload) == payload


def test_parser_accepts_json_fence_but_not_arbitrary_json():
    fenced = "```json\n{\"__W2A_TOOL_CALL__\":true,\"name\":\"bash\",\"arguments\":{\"cmd\":\"pwd\"}}\n```"
    assert OpenCodeBridge._parse_tool_call(fenced) == ("bash", {"cmd": "pwd"})
    assert OpenCodeBridge._parse_tool_call('{"name":"bash","arguments":{}}') is None
