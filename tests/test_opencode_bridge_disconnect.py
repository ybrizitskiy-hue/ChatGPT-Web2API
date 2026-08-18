import asyncio
import time

import pytest

from chatgpt_web2api.opencode_bridge import _CachedResult
from chatgpt_web2api.opencode_bridge_runtime import RuntimeOpenCodeBridge


@pytest.mark.asyncio
async def test_cancelled_client_does_not_cancel_shared_upstream_and_retry_replays(monkeypatch):
    bridge = RuntimeOpenCodeBridge(cache_ttl=30)
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def fake_upstream(body, auth_header):
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return _CachedResult(
            expires_at=time.monotonic() + 30,
            status=200,
            headers={},
            payload={"choices": [{"message": {"content": "done"}}]},
        )

    monkeypatch.setattr(bridge, "_do_upstream", fake_upstream)
    body = {"model": "auto", "messages": [{"role": "user", "content": "slow"}]}
    key = bridge._fingerprint(body, "Bearer local")

    disconnected = asyncio.create_task(bridge._get_result(key, body, "Bearer local"))
    await started.wait()
    disconnected.cancel()
    with pytest.raises(asyncio.CancelledError):
        await disconnected

    release.set()
    for _ in range(100):
        if key in bridge._cache:
            break
        await asyncio.sleep(0.01)

    replay = await bridge._get_result(key, body, "Bearer local")
    assert replay.status == 200
    assert replay.payload["choices"][0]["message"]["content"] == "done"
    assert calls == 1


@pytest.mark.asyncio
async def test_completed_server_error_is_retried_even_before_done_callback_runs(monkeypatch):
    bridge = RuntimeOpenCodeBridge(cache_ttl=30)
    calls = 0

    async def fake_upstream(body, auth_header):
        nonlocal calls
        calls += 1
        return _CachedResult(
            expires_at=time.monotonic() + 30,
            status=503,
            headers={},
            payload={"error": {"message": "temporary"}},
            cacheable=False,
        )

    monkeypatch.setattr(bridge, "_do_upstream", fake_upstream)
    body = {"model": "auto", "messages": [{"role": "user", "content": "retry"}]}
    key = bridge._fingerprint(body, "Bearer local")

    first = await bridge._get_result(key, body, "Bearer local")
    second = await bridge._get_result(key, body, "Bearer local")

    assert first.status == second.status == 503
    assert calls == 2
    assert key not in bridge._cache
