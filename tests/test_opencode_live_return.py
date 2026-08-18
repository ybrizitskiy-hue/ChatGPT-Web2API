import asyncio
import json
import time
from pathlib import Path

import pytest
from aiohttp import ClientSession, web

from chatgpt_web2api.opencode_bridge import OpenCodeBridge, _CachedResult
from chatgpt_web2api.opencode_bridge_runtime import (
    BRIDGE_APP_KEY,
    RuntimeOpenCodeBridge,
    create_app,
)
from chatgpt_web2api.opencode_setup import parser
from chatgpt_web2api.opencode_setup_common import (
    OPENCODE_STREAM_IDLE_TIMEOUT,
    configure_core,
)


async def _start_site(app):
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, f"http://127.0.0.1:{port}"


def _completion(content="ok"):
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 1,
        "model": "auto",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
    }


def test_runtime_parser_accepts_string_true_sentinel():
    text = json.dumps(
        {
            "__W2A_TOOL_CALL__": "true",
            "name": "read",
            "arguments": {"path": "README.md"},
            "extra": "ignored",
        }
    )
    assert RuntimeOpenCodeBridge._parse_tool_call(text) == (
        "read",
        {"path": "README.md"},
    )


def test_runtime_sse_begins_with_assistant_role():
    lines = [
        line[6:]
        for line in RuntimeOpenCodeBridge._as_sse(_completion()).decode().splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]
    first = json.loads(lines[0])
    assert first["choices"][0]["delta"] == {"role": "assistant"}


def test_runtime_replay_cache_is_bounded():
    bridge = RuntimeOpenCodeBridge(cache_max_entries=2)
    for index in range(3):
        bridge._cache[str(index)] = _CachedResult(
            expires_at=time.monotonic() + 60,
            status=200,
            headers={},
            payload=_completion(str(index)),
        )
    bridge._trim_cache_locked()
    assert list(bridge._cache) == ["1", "2"]


@pytest.mark.asyncio
async def test_runtime_repairs_one_model_tool_protocol_failure(monkeypatch):
    calls = []

    async def fake_base_do_upstream(self, body, auth_header):
        calls.append(body)
        if len(calls) == 1:
            return _CachedResult(
                expires_at=time.monotonic() + 60,
                status=502,
                headers={},
                payload={
                    "error": {
                        "message": "unknown tool",
                        "type": "server_error",
                        "code": "unknown_tool_call",
                    }
                },
            )
        return _CachedResult(
            expires_at=time.monotonic() + 60,
            status=200,
            headers={},
            payload={
                **_completion(None),
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_ok",
                                    "type": "function",
                                    "function": {"name": "read", "arguments": "{}"},
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
            },
        )

    monkeypatch.setattr(OpenCodeBridge, "_do_upstream", fake_base_do_upstream)
    bridge = RuntimeOpenCodeBridge()
    body = {
        "model": "auto",
        "messages": [{"role": "user", "content": "read the file"}],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "read",
                    "description": "Read a file",
                    "parameters": {"type": "object"},
                },
            }
        ],
        "tool_choice": "required",
    }
    result = await bridge._do_upstream(body, "Bearer test")
    assert result.status == 200
    assert len(calls) == 2
    system_text = "\n".join(
        str(message.get("content", ""))
        for message in calls[1]["messages"]
        if message.get("role") == "system"
    )
    assert "tool protocol repair" in system_text.lower()


@pytest.mark.asyncio
async def test_runtime_bridge_auth_is_enforced_when_configured():
    runner, url = await _start_site(
        create_app(upstream="http://127.0.0.1:1", api_key="local-secret")
    )
    try:
        async with ClientSession() as client:
            async with client.get(f"{url}/bridge/health") as response:
                assert response.status == 401
            async with client.get(
                f"{url}/bridge/health",
                headers={"Authorization": "Bearer local-secret"},
            ) as response:
                assert response.status == 200
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_stream_sends_immediate_keepalive_before_buffered_result(monkeypatch):
    app = create_app(
        upstream="http://127.0.0.1:1",
        heartbeat_interval=0.1,
    )
    bridge = app[BRIDGE_APP_KEY]

    async def slow_result(key, body, auth_header):
        await asyncio.sleep(0.25)
        return _CachedResult(
            expires_at=time.monotonic() + 60,
            status=200,
            headers={},
            payload=_completion("done"),
        )

    monkeypatch.setattr(bridge, "_get_result", slow_result)
    runner, url = await _start_site(app)
    try:
        async with ClientSession() as client:
            async with client.post(
                f"{url}/v1/chat/completions",
                json={
                    "model": "auto",
                    "messages": [{"role": "user", "content": "hello"}],
                    "stream": True,
                },
            ) as response:
                assert response.status == 200
                first = await response.content.readline()
                assert first == b": w2a-connected\n"
                rest = (await response.read()).decode("utf-8")
                assert ": w2a-keepalive" in rest
                assert '"role":"assistant"' in rest
                assert "data: [DONE]" in rest
    finally:
        await runner.cleanup()


def test_setup_shortens_old_post_content_idle_budgets(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "detector_reasoning_stream_idle_timeout_seconds": 120,
                "detector_default_stream_idle_timeout_seconds": 90,
            }
        ),
        encoding="utf-8",
    )
    configure_core(path, "key", "http://127.0.0.1:8080", "auto")
    config = json.loads(path.read_text(encoding="utf-8"))
    assert config["detector_reasoning_stream_idle_timeout_seconds"] == (
        OPENCODE_STREAM_IDLE_TIMEOUT
    )
    assert config["detector_default_stream_idle_timeout_seconds"] == (
        OPENCODE_STREAM_IDLE_TIMEOUT
    )


def test_serve_parser_exposes_bridge_api_key_file(tmp_path):
    key_file = Path(tmp_path) / "bridge-key"
    args = parser().parse_args(["serve", "--api-key-file", str(key_file)])
    assert args.api_key_file == key_file
