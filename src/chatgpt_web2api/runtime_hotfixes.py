"""Narrow runtime recoveries for browser/CDP edge cases seen in live OpenCode use.

These patches deliberately reconcile observable browser state after an ambiguous
CDP timeout instead of blindly retrying a navigation or chat send.  They are
kept in one small module so the behavior is easy to remove once the underlying
CDP implementation absorbs the same logic directly.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

NAVIGATION_RECONCILE_TIMEOUT_SECONDS = 20.0
MODEL_CATALOG_PROBE_TIMEOUT_SECONDS = 3.0
_INSTALLED = False


def _is_page_navigate_timeout(exc: BaseException) -> bool:
    return isinstance(exc, TimeoutError) and "CDP timeout: Page.navigate" in str(exc)


async def _new_chat_is_ready(driver: Any) -> bool:
    """Observe whether the live tab is already usable after a navigation timeout."""
    try:
        if not await driver._has_composer():
            return False
        url = await driver._js("location.href", timeout=5)
    except Exception:
        return False
    return isinstance(url, str) and "chatgpt.com" in url.lower()


async def _conversation_is_ready(driver: Any, conversation_id: str) -> bool:
    try:
        if not await driver._is_live_conversation_url(conversation_id):
            return False
        return bool(await driver._has_composer())
    except Exception:
        return False


async def _wait_until(predicate, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await predicate():
            return True
        await asyncio.sleep(0.5)
    return False


def install_runtime_hotfixes() -> None:
    """Install idempotent state-reconciliation guards on the production classes."""
    global _INSTALLED
    if _INSTALLED:
        return

    from aiohttp import web

    from .api_server import APIServer
    from .cdp_driver import CDPDriver

    original_navigate_new_chat = CDPDriver.navigate_new_chat
    original_navigate_conversation = CDPDriver.navigate_conversation

    async def navigate_new_chat_reconciled(self, gizmo_id: str | None = None) -> None:
        try:
            await original_navigate_new_chat(self, gizmo_id)
            return
        except TimeoutError as exc:
            if not _is_page_navigate_timeout(exc):
                raise
            logger.warning(
                "Page.navigate timed out while opening a new chat; reconciling live page state"
            )
            if await _wait_until(
                lambda: _new_chat_is_ready(self), NAVIGATION_RECONCILE_TIMEOUT_SECONDS
            ):
                # The CDP command response was lost/late, but the browser did
                # complete the transition.  Do not navigate again: a second
                # navigation can create another chat/tab and is unnecessary.
                self._current_conv_id = None
                logger.info("Recovered Page.navigate timeout: new-chat composer is ready")
                return
            raise

    async def navigate_conversation_reconciled(self, conversation_id: str) -> None:
        try:
            await original_navigate_conversation(self, conversation_id)
            return
        except TimeoutError as exc:
            if not _is_page_navigate_timeout(exc):
                raise
            logger.warning(
                "Page.navigate timed out for conversation %s; reconciling live page state",
                conversation_id,
            )
            if await _wait_until(
                lambda: _conversation_is_ready(self, conversation_id),
                NAVIGATION_RECONCILE_TIMEOUT_SECONDS,
            ):
                self._current_conv_id = conversation_id
                logger.info(
                    "Recovered Page.navigate timeout: conversation %s is ready",
                    conversation_id,
                )
                return
            raise

    async def bounded_models(self, request):
        """Keep /v1/models responsive even when ChatGPT's backend model fetch stalls."""
        if err := self._check_auth(request):
            return err

        task = getattr(self, "_w2a_model_catalog_task", None)
        if task is None:
            task = asyncio.create_task(self._driver.get_models())
            self._w2a_model_catalog_task = task

        source = "live"
        try:
            raw = await asyncio.wait_for(
                asyncio.shield(task), timeout=MODEL_CATALOG_PROBE_TIMEOUT_SECONDS
            )
            self._w2a_model_catalog_task = None
        except TimeoutError:
            # Do not cancel the CDP operation: cancellation can leave a pending
            # CDP future behind.  Let the single shared task finish in the
            # background and serve the stable fallback catalog immediately.
            logger.warning(
                "Model catalog probe exceeded %.1fs; serving fallback catalog",
                MODEL_CATALOG_PROBE_TIMEOUT_SECONDS,
            )
            raw = []
            source = "fallback-timeout"
        except Exception as exc:
            logger.warning("Model catalog probe failed: %s; serving fallback catalog", exc)
            self._w2a_model_catalog_task = None
            raw = []
            source = "fallback-error"

        models = []
        if isinstance(raw, list):
            for item in raw:
                if not isinstance(item, dict):
                    continue
                slug = item.get("slug")
                if not isinstance(slug, str) or not slug:
                    continue
                models.append(
                    {
                        "id": slug,
                        "object": "model",
                        "created": 1700000000,
                        "owned_by": "chatgpt-web",
                    }
                )

        if not models:
            for slug in ("auto", "gpt-5-5", "gpt-5-mini"):
                models.append(
                    {
                        "id": slug,
                        "object": "model",
                        "created": 1700000000,
                        "owned_by": "chatgpt-web",
                    }
                )

        return web.json_response(
            {"object": "list", "data": models},
            headers={"X-Web2API-Model-Catalog": source},
        )

    CDPDriver.navigate_new_chat = navigate_new_chat_reconciled
    CDPDriver.navigate_conversation = navigate_conversation_reconciled
    APIServer._handle_models = bounded_models
    _INSTALLED = True
