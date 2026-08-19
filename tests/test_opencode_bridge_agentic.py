import time

import pytest

from chatgpt_web2api.opencode_bridge import _CachedResult
from chatgpt_web2api.opencode_bridge_agentic import AgenticOpenCodeBridge
from chatgpt_web2api.opencode_bridge_runtime import RuntimeOpenCodeBridge


def _tool(name: str = "write") -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": f"OpenCode {name} tool",
            "parameters": {"type": "object"},
        },
    }


def _result(text: str) -> _CachedResult:
    return _CachedResult(
        expires_at=time.monotonic() + 60,
        status=200,
        headers={},
        payload={
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": "stop",
                }
            ]
        },
    )


def test_tool_instructions_explicitly_describe_local_capability():
    body = {"tools": [_tool("write")], "tool_choice": "auto"}
    text = AgenticOpenCodeBridge._tool_instructions(body["tools"], body)
    assert "real OpenCode capabilities on the user's local machine" in text
    assert "request the tool instead of claiming" in text


def test_actual_failed_edit_prompt_is_recognized_as_local_tool_intent():
    body = {
        "messages": [
            {
                "role": "user",
                "content": (
                    "Create a file named opencode_edit_test.txt with version-1. "
                    "Then use the appropriate file editing tool to change it to version-2, "
                    "read it back, and delete the file."
                ),
            }
        ],
        "tools": [_tool("write"), _tool("edit"), _tool("read")],
        "tool_choice": "auto",
    }
    assert AgenticOpenCodeBridge._explicit_local_tool_intent(body)


@pytest.mark.asyncio
async def test_false_filesystem_denial_gets_one_required_tool_retry(monkeypatch):
    calls: list[dict] = []
    replies = [
        _result("I can’t access or modify the OpenCode filesystem from this environment."),
        _result("recovered"),
    ]

    async def fake_parent(self, body, auth_header):
        calls.append(body)
        return replies[len(calls) - 1]

    monkeypatch.setattr(RuntimeOpenCodeBridge, "_do_upstream", fake_parent)
    bridge = AgenticOpenCodeBridge()
    body = {
        "model": "auto",
        "messages": [
            {
                "role": "user",
                "content": "Create a file named test.txt and then edit the file.",
            }
        ],
        "tools": [_tool("write"), _tool("edit")],
        "tool_choice": "auto",
    }

    result = await bridge._do_upstream(body, "Bearer local")

    assert result is replies[1]
    assert len(calls) == 2
    assert calls[0]["tool_choice"] == "auto"
    assert calls[1]["tool_choice"] == "required"
    system = next(message for message in calls[1]["messages"] if message["role"] == "system")
    assert "OpenCode local-tool correction" in system["content"]
    assert "MUST now request exactly one appropriate available tool" in system["content"]


@pytest.mark.asyncio
async def test_denial_is_not_retried_without_explicit_local_action(monkeypatch):
    calls: list[dict] = []
    reply = _result("I cannot access files from this environment.")

    async def fake_parent(self, body, auth_header):
        calls.append(body)
        return reply

    monkeypatch.setattr(RuntimeOpenCodeBridge, "_do_upstream", fake_parent)
    bridge = AgenticOpenCodeBridge()
    body = {
        "model": "auto",
        "messages": [{"role": "user", "content": "Explain sandbox security limitations."}],
        "tools": [_tool("read")],
        "tool_choice": "auto",
    }

    result = await bridge._do_upstream(body, None)

    assert result is reply
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_tool_choice_none_never_triggers_environment_retry(monkeypatch):
    calls: list[dict] = []
    reply = _result("I cannot modify the filesystem from this environment.")

    async def fake_parent(self, body, auth_header):
        calls.append(body)
        return reply

    monkeypatch.setattr(RuntimeOpenCodeBridge, "_do_upstream", fake_parent)
    bridge = AgenticOpenCodeBridge()
    body = {
        "model": "auto",
        "messages": [{"role": "user", "content": "Edit the file test.txt."}],
        "tools": [_tool("edit")],
        "tool_choice": "none",
    }

    result = await bridge._do_upstream(body, None)

    assert result is reply
    assert len(calls) == 1
