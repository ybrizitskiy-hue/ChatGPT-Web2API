import asyncio

import pytest
from aiohttp.test_utils import make_mocked_request

from chatgpt_web2api import runtime_hotfixes
from chatgpt_web2api.api_server import APIServer
from chatgpt_web2api.cdp_driver import CDPDriver
from chatgpt_web2api.config import Config


def test_new_chat_reconciliation_requires_actual_new_chat_route():
    assert runtime_hotfixes._is_expected_new_chat_url(
        "https://chatgpt.com/?model=auto", None
    )
    assert runtime_hotfixes._is_expected_new_chat_url("https://chatgpt.com/", None)
    assert not runtime_hotfixes._is_expected_new_chat_url(
        "https://chatgpt.com/c/existing-conversation", None
    )
    assert runtime_hotfixes._is_expected_new_chat_url(
        "https://chatgpt.com/g/project-123/project", "project-123"
    )
    assert not runtime_hotfixes._is_expected_new_chat_url(
        "https://chatgpt.com/g/other-project/project", "project-123"
    )


def test_runtime_composer_selectors_cover_current_editor_families():
    from chatgpt_web2api import cdp_driver, chatgpt_dom

    selector = runtime_hotfixes.LIVE_COMPOSER_SELECTOR
    fallback = runtime_hotfixes.LIVE_COMPOSER_FALLBACK_SELECTOR

    assert "data-lexical-editor" in selector
    assert "contenteditable" in selector
    assert "ProseMirror" in selector
    assert "aria-placeholder" in selector
    assert "wcDTda_fallbackTextarea" in fallback
    assert chatgpt_dom.COMPOSER_SELECTOR == selector
    assert cdp_driver.COMPOSER_SELECTOR == selector
    assert chatgpt_dom.COMPOSER_FALLBACK_SELECTOR == fallback
    assert cdp_driver.COMPOSER_FALLBACK_SELECTOR == fallback


@pytest.mark.asyncio
async def test_visible_composer_probe_is_visibility_aware(monkeypatch):
    driver = CDPDriver(tab_mode="adopt")
    expressions = []

    async def js(expr, timeout=15):
        expressions.append(expr)
        return '{"ready": true, "tag": "DIV", "lexical": "true"}'

    monkeypatch.setattr(driver, "_js", js)

    assert await driver._has_composer() is True
    probe = expressions[-1]
    assert "getBoundingClientRect" in probe
    assert "data-lexical-editor" in probe
    assert "aria-disabled" in probe
    assert "wcDTda_fallbackTextarea" in probe


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
async def test_new_chat_success_without_composer_is_rejected(monkeypatch):
    """Upstream navigation used to fall through silently after 30 failed probes."""
    import chatgpt_web2api.cdp_driver as driver_module
    from chatgpt_web2api.cdp_driver import SendReadinessError

    driver = CDPDriver(tab_mode="adopt")
    navigate_calls = 0

    async def successful_cdp(method, params=None, timeout=15, _retry=True):
        nonlocal navigate_calls
        assert method == "Page.navigate"
        navigate_calls += 1
        return {"result": {}}

    async def never_ready_js(expr, timeout=15):
        if expr == "location.href":
            return "https://chatgpt.com/?model=auto"
        return '{"ready": false, "url": "https://chatgpt.com/?model=auto"}'

    async def no_composer():
        return False

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr(driver, "_cdp", successful_cdp)
    monkeypatch.setattr(driver, "_js", never_ready_js)
    monkeypatch.setattr(driver, "_has_composer", no_composer)
    monkeypatch.setattr(driver, "_capture_selector_diagnostic", no_sleep)
    monkeypatch.setattr(driver_module.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(runtime_hotfixes, "POST_NAVIGATION_COMPOSER_TIMEOUT_SECONDS", 0.001)

    with pytest.raises(SendReadinessError, match="no visible writable composer"):
        await driver.navigate_new_chat()

    assert navigate_calls == 1


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
