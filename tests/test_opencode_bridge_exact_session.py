import time

import pytest

from chatgpt_web2api.opencode_bridge import _CachedResult
from chatgpt_web2api.opencode_bridge_exact_session import (
    ExactSessionOpenCodeBridge,
    _SESSION_CONTEXT,
)
from chatgpt_web2api.opencode_bridge_runtime import RuntimeOpenCodeBridge


def _tool(name: str) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": f"OpenCode {name} tool",
            "parameters": {"type": "object"},
        },
    }


def _body(messages: list[dict]) -> dict:
    return {
        "model": "auto",
        "messages": messages,
        "tools": [_tool("bash"), _tool("read")],
        "tool_choice": "auto",
    }


def _result(text: str = "ok", conversation_id: str = "conv-1") -> _CachedResult:
    return _CachedResult(
        expires_at=time.monotonic() + 60,
        status=200,
        headers={},
        payload={
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


def test_fingerprint_is_scoped_by_exact_opencode_session():
    body = _body([{"role": "user", "content": "same"}])
    token = _SESSION_CONTEXT.set("session-A")
    try:
        first = ExactSessionOpenCodeBridge._fingerprint(body, "Bearer local")
    finally:
        _SESSION_CONTEXT.reset(token)
    token = _SESSION_CONTEXT.set("session-B")
    try:
        second = ExactSessionOpenCodeBridge._fingerprint(body, "Bearer local")
    finally:
        _SESSION_CONTEXT.reset(token)
    assert first != second


@pytest.mark.asyncio
async def test_exact_session_continues_same_chat(monkeypatch):
    calls: list[dict] = []
    replies = [_result("one", "conv-A"), _result("two", "conv-A")]

    async def fake_parent(self, body, auth_header):
        calls.append(body)
        return replies.pop(0)

    monkeypatch.setattr(RuntimeOpenCodeBridge, "_do_upstream", fake_parent)
    bridge = ExactSessionOpenCodeBridge()
    token = _SESSION_CONTEXT.set("session-1")
    try:
        await bridge._do_upstream(
            _body(
                [
                    {"role": "system", "content": "system"},
                    {"role": "user", "content": "task"},
                ]
            ),
            "Bearer local",
        )
        await bridge._do_upstream(
            _body(
                [
                    {"role": "system", "content": "system"},
                    {"role": "user", "content": "task"},
                    {"role": "assistant", "content": None},
                    {"role": "tool", "tool_call_id": "call-1", "content": "result"},
                ]
            ),
            "Bearer local",
        )
    finally:
        _SESSION_CONTEXT.reset(token)

    assert calls[1]["conversation_id"] == "conv-A"
    assert calls[1]["messages"] == [
        {"role": "tool", "tool_call_id": "call-1", "content": "result"}
    ]


@pytest.mark.asyncio
async def test_exact_session_survives_history_compaction(monkeypatch):
    calls: list[dict] = []
    replies = [_result("one", "conv-A"), _result("two", "conv-A")]

    async def fake_parent(self, body, auth_header):
        calls.append(body)
        return replies.pop(0)

    monkeypatch.setattr(RuntimeOpenCodeBridge, "_do_upstream", fake_parent)
    bridge = ExactSessionOpenCodeBridge()
    token = _SESSION_CONTEXT.set("session-1")
    try:
        await bridge._do_upstream(
            _body(
                [
                    {"role": "system", "content": "long original system"},
                    {"role": "user", "content": "long original task"},
                ]
            ),
            "Bearer local",
        )
        await bridge._do_upstream(
            _body(
                [
                    {"role": "system", "content": "compacted system"},
                    {"role": "user", "content": "continue after compaction"},
                ]
            ),
            "Bearer local",
        )
    finally:
        _SESSION_CONTEXT.reset(token)

    assert calls[1]["conversation_id"] == "conv-A"
    assert calls[1]["messages"] == [
        {"role": "user", "content": "continue after compaction"}
    ]


@pytest.mark.asyncio
async def test_same_prompt_in_different_opencode_sessions_stays_separate(monkeypatch):
    calls: list[dict] = []
    replies = [_result("one", "conv-A"), _result("two", "conv-B")]

    async def fake_parent(self, body, auth_header):
        calls.append(body)
        return replies.pop(0)

    monkeypatch.setattr(RuntimeOpenCodeBridge, "_do_upstream", fake_parent)
    bridge = ExactSessionOpenCodeBridge()
    token = _SESSION_CONTEXT.set("session-A")
    try:
        await bridge._do_upstream(
            _body(
                [
                    {"role": "system", "content": "system"},
                    {"role": "user", "content": "same prompt"},
                ]
            ),
            "Bearer local",
        )
    finally:
        _SESSION_CONTEXT.reset(token)

    token = _SESSION_CONTEXT.set("session-B")
    try:
        await bridge._do_upstream(
            _body(
                [
                    {"role": "system", "content": "system"},
                    {"role": "user", "content": "same prompt"},
                    {"role": "assistant", "content": "one"},
                    {"role": "user", "content": "next"},
                ]
            ),
            "Bearer local",
        )
    finally:
        _SESSION_CONTEXT.reset(token)

    assert "conversation_id" not in calls[1]
