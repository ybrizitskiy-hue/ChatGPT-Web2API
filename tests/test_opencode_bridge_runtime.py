import pytest
from aiohttp import ClientSession, web

from chatgpt_web2api.opencode_bridge_runtime import RuntimeOpenCodeBridge, create_app


async def _start_site(app):
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, f"http://127.0.0.1:{port}"


def test_idempotency_header_changes_runtime_fingerprint():
    body = {"model": "auto", "messages": [{"role": "user", "content": "hello"}]}
    assert RuntimeOpenCodeBridge._fingerprint(body, "Bearer x", "turn-1") != (
        RuntimeOpenCodeBridge._fingerprint(body, "Bearer x", "turn-2")
    )


@pytest.mark.asyncio
async def test_runtime_health_and_get_proxy_preserve_query_and_auth():
    seen = {}

    async def models(request):
        seen["query"] = request.query_string
        seen["auth"] = request.headers.get("Authorization")
        return web.json_response({"object": "list", "data": []})

    upstream = web.Application()
    upstream.router.add_get("/v1/models", models)
    upstream_runner, upstream_url = await _start_site(upstream)
    bridge_runner, bridge_url = await _start_site(create_app(upstream=upstream_url))
    try:
        async with ClientSession() as client:
            async with client.get(f"{bridge_url}/bridge/health") as response:
                assert response.status == 200
                assert (await response.json())["status"] == "healthy"
            async with client.get(
                f"{bridge_url}/v1/models?fresh=1",
                headers={"Authorization": "Bearer local"},
            ) as response:
                assert response.status == 200
                assert await response.json() == {"object": "list", "data": []}
        assert seen == {"query": "fresh=1", "auth": "Bearer local"}
    finally:
        await bridge_runner.cleanup()
        await upstream_runner.cleanup()


@pytest.mark.asyncio
async def test_runtime_invalid_json_and_dead_upstream_are_structured_errors():
    bridge_runner, bridge_url = await _start_site(
        create_app(upstream="http://127.0.0.1:1", request_timeout=1)
    )
    try:
        async with ClientSession() as client:
            async with client.post(
                f"{bridge_url}/v1/chat/completions",
                data="not-json",
                headers={"Content-Type": "application/json"},
            ) as response:
                assert response.status == 400
                assert (await response.json())["error"]["code"] == "invalid_json"
            async with client.post(
                f"{bridge_url}/v1/chat/completions",
                json={"model": "auto", "messages": [{"role": "user", "content": "hi"}]},
            ) as response:
                assert response.status == 502
                payload = await response.json()
                assert payload["error"]["code"] == "upstream_connection_error"
    finally:
        await bridge_runner.cleanup()
