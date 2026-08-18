import json

import pytest
from aiohttp import ClientSession, web

from chatgpt_web2api.opencode_bridge import OpenCodeBridge


def _tool(name="read"):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": "Read a file",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    }


async def _start_site(app):
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, f"http://127.0.0.1:{port}"


@pytest.mark.asyncio
async def test_http_roundtrip_translates_opencode_tools_to_sse_tool_call():
    seen = {}

    async def upstream_chat(request):
        seen["body"] = await request.json()
        return web.json_response(
            {
                "id": "chatcmpl-upstream",
                "object": "chat.completion",
                "created": 1,
                "model": "gpt-5",
                "conversation_id": "conv-1",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": (
                                '{"__W2A_TOOL_CALL__":true,"name":"read",'
                                '"arguments":{"path":"README.md"}}'
                            ),
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            }
        )

    upstream_app = web.Application()
    upstream_app.router.add_post("/v1/chat/completions", upstream_chat)
    upstream_runner, upstream_url = await _start_site(upstream_app)

    bridge = OpenCodeBridge(upstream=upstream_url, cache_ttl=30)
    bridge_app = web.Application()
    bridge_app.on_startup.append(bridge.start)
    bridge_app.on_cleanup.append(bridge.close)
    bridge_app.router.add_post("/v1/chat/completions", bridge.chat)
    bridge_runner, bridge_url = await _start_site(bridge_app)

    try:
        request_body = {
            "model": "gpt-5",
            "messages": [{"role": "user", "content": "Read README.md"}],
            "tools": [_tool("read")],
            "tool_choice": "auto",
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        async with ClientSession() as client:
            async with client.post(
                f"{bridge_url}/v1/chat/completions",
                json=request_body,
                headers={"Authorization": "Bearer local"},
            ) as response:
                assert response.status == 200
                raw = await response.text()

        upstream_body = seen["body"]
        assert upstream_body["stream"] is False
        assert "tools" not in upstream_body
        assert "tool_choice" not in upstream_body
        assert "stream_options" not in upstream_body
        assert "__W2A_TOOL_CALL__" in upstream_body["messages"][0]["content"]

        events = [
            json.loads(line[6:])
            for line in raw.splitlines()
            if line.startswith("data: {")
        ]
        tool = events[0]["choices"][0]["delta"]["tool_calls"][0]
        assert tool["index"] == 0
        assert tool["type"] == "function"
        assert tool["function"]["name"] == "read"
        assert json.loads(tool["function"]["arguments"]) == {"path": "README.md"}
        assert events[1]["choices"][0]["finish_reason"] == "tool_calls"
        assert events[-1]["usage"]["total_tokens"] == 0
        assert raw.rstrip().endswith("data: [DONE]")
    finally:
        await bridge_runner.cleanup()
        await upstream_runner.cleanup()
