import asyncio

import pytest
from aiohttp.test_utils import make_mocked_request

from chatgpt_web2api.api_server import APIServer
from chatgpt_web2api.cdp_driver import CDPDriver
from chatgpt_web2api.config import Config
from chatgpt_web2api import runtime_hotfixes


@pytest.mark.asyncio
async def test_new_chat_page_navigate_timeout_reconciles_without_second_navigation(monkeypatch):
    driver = CDPDriver(tab_mode="adopt")
    navigate_calls = 0

    async def timed_out_cdp(method, params=None, timeout=15, _retry=True):
        nonlocal navigate_calls
        assert method == "Page.navigate"
        navigate_calls += 1
        raise TimeoutError("CDP timeout: Page.navigate")

    async def has_composer():
        return True

    async def live_url(expr, timeout=15):
        assert expr == "location.href"
        return "https://chatgpt.com/?model=auto"

    monkeypatch.setattr(driver, "_cdp", timed_out_cdp)
    monkeypatch.setattr(driver, "_has_composer", has_composer)
    monkeypatch.setattr(driver, "_js", live_url)

    await driver.navigate_new_chat()

    assert navigate_calls == 1
    assert driver._current_conv_id is None


@pytest.mark.asyncio
async def test_conversation_page_navigate_timeout_reconciles_live_target(monkeypatch):
    driver = CDPDriver(tab_mode="adopt")
    navigate_calls = 0

    async def timed_out_cdp(method, params=None, timeout=15, _retry=True):
        nonlocal navigate_calls
        assert method == "Page.navigate"
        navigate_calls += 1
        raise TimeoutError("CDP timeout: Page.navigate")

    async def live_conversation(conversation_id):
        return conversation_id == "conv-123"

    async def has_composer():
        return True

    monkeypatch.setattr(driver, "_cdp", timed_out_cdp)
    monkeypatch.setattr(driver, "_is_live_conversation_url", live_conversation)
    monkeypatch.setattr(driver, "_has_composer", has_composer)

    await driver.navigate_conversation("conv-123")

    assert navigate_calls == 1
    assert driver._current_conv_id == "conv-123"


@pytest.mark.asyncio
async def test_model_catalog_timeout_returns_fallback_without_cancelling_probe(monkeypatch):
    config = Config()

    class SlowDriver:
        async def get_models(self):
            await asyncio.sleep(0.05)
            return [{"slug": "late-live-model"}]

    server = APIServer(config, SlowDriver())
    monkeypatch.setattr(runtime_hotfixes, "MODEL_CATALOG_PROBE_TIMEOUT_SECONDS", 0.01)

    response = await server._handle_models(make_mocked_request("GET", "/v1/models"))

    assert response.status == 200
    assert response.headers["X-Web2API-Model-Catalog"] == "fallback-timeout"
    assert b'"id": "auto"' in response.body
    task = server._w2a_model_catalog_task
    assert not task.cancelled()
    await task
    assert task.result() == [{"slug": "late-live-model"}]
