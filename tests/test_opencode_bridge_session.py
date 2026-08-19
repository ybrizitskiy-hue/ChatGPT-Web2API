import time

import pytest

from chatgpt_web2api.opencode_bridge import _CachedResult
from chatgpt_web2api.opencode_bridge_runtime import RuntimeOpenCodeBridge
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
            "parameters": {"type": "object"},
        },
    }


def _result(text: str = "ok", conversation_id: str = "conv-1") -> _CachedResult:
    return _CachedResult(
        expires_at=time.monotonic() + 60,
        status=200,
        headers={},
        payload={
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "created": 1,
            "model": "auto",
            "conversation_id": conversation_id,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": "stop",
                }
            ],
        },
    )


def _body(messages: list[dict], tools: list[dict] | None = None) -> dict:
    return {
        "model": "auto",
        "stream": False,
        "messages": messages,
        "tools": tools if tools is not None else [_tool("bash"), _tool("read")],
        "tool_choice": "auto",
    }


def test_tool_prompt_forbids_native_chatgpt_browser_and_connectors():
    body = _body([{"role": "user", "content": "Inspect the repo."}])
    text = SessionOpenCodeBridge._tool_instructions(body["tools"], body)
    assert "NEVER invoke ChatGPT-native web browsing" in text
    assert "GitHub authorization flows" in text
    assert "permission/confirmation prompt" in text
    assert "bash with git/gh" in text


def test_explicit_github_user_action_forces_an_opencode_tool():
    body = _body([{"role": "user", "content": "Go to GitHub and inspect PR 12."}])
    forced = SessionOpenCodeBridge._force_opencode_tool_for_github(body)
    assert forced is not body
    assert forced["tool_choice"] == "required"


def test_tool_result_turn_does_not_force_another_tool():
    body = _body(
        [
            {"role": "user", "content": "Go to GitHub and inspect PR 12."},
            {"role": "assistant", "content": None},
            {"role": "tool", "tool_call_id": "call-1", "content": "done"},
        ]
    )
    assert SessionOpenCodeBridge._force_opencode_tool_for_github(body) is body
    assert body["tool_choice"] == "auto"


@pytest.mark.asyncio
async def test_title_generator_is_answered_locally_without_chatgpt_send(monkeypatch):
    async def fail_parent(self, body, auth_header):
        raise AssertionError("title generation must not reach Web2API")

    monkeypatch.setattr(RuntimeOpenCodeBridge, "_do_upstream", fail_parent)
    bridge = SessionOpenCodeBridge()
    body = _body(
        [
            {
                "role": "system",
                "content": "You are a title generator. You output ONLY a thread title.",
            },
            {
                "role": "user",
                "content": "Generate a title for this conversation:\n[User]\nhello world",
            },
        ],
        tools=[],
    )

    result = await bridge._do_upstream(body, "Bearer local")

    assert result.status == 200
    assert result.payload["choices"][0]["message"]["content"] == "hello world"
    assert "conversation_id" not in result.payload


@pytest.mark.asyncio
async def test_tool_result_continues_same_chat_with_delta_only(monkeypatch):
    calls: list[dict] = []
    replies = [_result("first", "conv-A"), _result("second", "conv-A")]

    async def fake_parent(self, body, auth_header):
        calls.append(body)
        return replies.pop(0)

    monkeypatch.setattr(RuntimeOpenCodeBridge, "_do_upstream", fake_parent)
    bridge = SessionOpenCodeBridge()
    first = _body(
        [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "Use bash and then read the result."},
        ]
    )
    await bridge._do_upstream(first, "Bearer local")

    second = _body(
        [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "Use bash and then read the result."},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "bash", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call-1", "content": "bash-result"},
        ]
    )
    await bridge._do_upstream(second, "Bearer local")

    continuation = calls[1]
    assert continuation["conversation_id"] == "conv-A"
    assert continuation[_CONTINUATION_MARKER] is True
    assert continuation["messages"] == [
        {"role": "tool", "tool_call_id": "call-1", "content": "bash-result"}
    ]


@pytest.mark.asyncio
async def test_new_user_followup_continues_same_chat_without_replaying_assistant(monkeypatch):
    calls: list[dict] = []
    replies = [_result("done", "conv-A"), _result("next", "conv-A")]

    async def fake_parent(self, body, auth_header):
        calls.append(body)
        return replies.pop(0)

    monkeypatch.setattr(RuntimeOpenCodeBridge, "_do_upstream", fake_parent)
    bridge = SessionOpenCodeBridge()
    await bridge._do_upstream(
        _body(
            [
                {"role": "system", "content": "system"},
                {"role": "user", "content": "First task"},
            ]
        ),
        "Bearer local",
    )
    await bridge._do_upstream(
        _body(
            [
                {"role": "system", "content": "system"},
                {"role": "user", "content": "First task"},
                {"role": "assistant", "content": "done"},
                {"role": "user", "content": "Second task"},
            ]
        ),
        "Bearer local",
    )

    continuation = calls[1]
    assert continuation["conversation_id"] == "conv-A"
    assert continuation["messages"] == [{"role": "user", "content": "Second task"}]


@pytest.mark.asyncio
async def test_unrelated_history_starts_a_new_chat(monkeypatch):
    calls: list[dict] = []
    replies = [_result("one", "conv-1"), _result("two", "conv-2")]

    async def fake_parent(self, body, auth_header):
        calls.append(body)
        return replies.pop(0)

    monkeypatch.setattr(RuntimeOpenCodeBridge, "_do_upstream", fake_parent)
    bridge = SessionOpenCodeBridge()
    await bridge._do_upstream(
        _body([{"role": "system", "content": "system"}, {"role": "user", "content": "one"}]),
        "Bearer local",
    )
    await bridge._do_upstream(
        _body(
            [
                {"role": "system", "content": "system"},
                {"role": "user", "content": "different session"},
            ]
        ),
        "Bearer local",
    )

    assert "conversation_id" not in calls[1]


@pytest.mark.asyncio
async def test_session_affinity_is_isolated_by_bridge_credential(monkeypatch):
    calls: list[dict] = []
    replies = [_result("one", "conv-1"), _result("two", "conv-2")]

    async def fake_parent(self, body, auth_header):
        calls.append(body)
        return replies.pop(0)

    monkeypatch.setattr(RuntimeOpenCodeBridge, "_do_upstream", fake_parent)
    bridge = SessionOpenCodeBridge()
    first = _body([{"role": "system", "content": "system"}, {"role": "user", "content": "one"}])
    await bridge._do_upstream(first, "Bearer A")
    expanded = _body(
        [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "one"},
            {"role": "assistant", "content": "one"},
            {"role": "user", "content": "two"},
        ]
    )
    await bridge._do_upstream(expanded, "Bearer B")

    assert "conversation_id" not in calls[1]


@pytest.mark.asyncio
async def test_environment_denial_retry_remains_in_same_chat(monkeypatch):
    calls: list[dict] = []
    replies = [
        _result("I can't access the filesystem from this environment.", "conv-R"),
        _result("recovered", "conv-R"),
    ]

    async def fake_parent(self, body, auth_header):
        calls.append(body)
        return replies.pop(0)

    monkeypatch.setattr(RuntimeOpenCodeBridge, "_do_upstream", fake_parent)
    bridge = SessionOpenCodeBridge()
    body = _body(
        [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "Create a file named test.txt."},
        ],
        tools=[_tool("write")],
    )

    result = await bridge._do_upstream(body, "Bearer local")

    assert result.payload["conversation_id"] == "conv-R"
    repair = calls[1]
    assert repair["conversation_id"] == "conv-R"
    assert repair[_CONTINUATION_MARKER] is True
    assert repair["tool_choice"] == "required"
    assert repair["messages"][0]["role"] == "user"
    assert "native ChatGPT browser" in repair["messages"][0]["content"]


def test_continuation_prepare_normalizes_tool_result_without_reinjecting_tools():
    body = _body([{"role": "tool", "tool_call_id": "call-1", "content": "result"}])
    body["conversation_id"] = "conv-A"
    body[_CONTINUATION_MARKER] = True

    upstream = SessionOpenCodeBridge._prepare_upstream_body(body)

    assert upstream["conversation_id"] == "conv-A"
    assert _CONTINUATION_MARKER not in upstream
    assert "tools" not in upstream
    assert "tool_choice" not in upstream
    assert upstream["messages"] == [
        {"role": "user", "content": "[Tool result call-1]\nresult"}
    ]
