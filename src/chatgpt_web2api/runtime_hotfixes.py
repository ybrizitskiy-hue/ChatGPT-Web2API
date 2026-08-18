"""Narrow runtime recoveries for browser/CDP edge cases seen in live OpenCode use.

These patches deliberately reconcile observable browser state after ambiguous
CDP timeouts instead of blindly retrying navigation or chat sends. They also
harden composer discovery against ChatGPT UI drift while keeping the change
isolated from the upstream driver implementation.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.parse
from typing import Any

logger = logging.getLogger(__name__)

NAVIGATION_RECONCILE_TIMEOUT_SECONDS = 20.0
POST_NAVIGATION_COMPOSER_TIMEOUT_SECONDS = 12.0
MODEL_CATALOG_PROBE_TIMEOUT_SECONDS = 3.0
_INSTALLED = False

# Current ChatGPT deployments have used both ProseMirror and Lexical-style
# contenteditable editors. Keep the selector structural and scoped so a search
# textbox elsewhere in the app is not mistaken for the prompt composer.
LIVE_COMPOSER_SELECTOR = ", ".join(
    (
        '#prompt-textarea[contenteditable="true"]',
        'div[contenteditable="true"][data-lexical-editor="true"]',
        'form [contenteditable="true"][role="textbox"]',
        'main [contenteditable="true"][role="textbox"]',
        'form div.ProseMirror[contenteditable="true"]',
        'main div.ProseMirror[contenteditable="true"]',
        'form [contenteditable="true"][aria-label*="message" i]',
        'form [contenteditable="true"][aria-placeholder*="message" i]',
        'form [contenteditable="true"][data-placeholder*="message" i]',
        'form [contenteditable="true"][data-placeholder*="ask" i]',
    )
)

# Explicitly exclude the known hidden JS-off fallback textarea. A hidden
# #prompt-textarea previously made readiness checks report false positives.
LIVE_COMPOSER_FALLBACK_SELECTOR = ", ".join(
    (
        'textarea#prompt-textarea:not(.wcDTda_fallbackTextarea)',
        'textarea[name="prompt-textarea"]:not(.wcDTda_fallbackTextarea)',
        'form textarea[placeholder*="message" i]',
        'form textarea[placeholder*="ask" i]',
    )
)

LIVE_SEND_BUTTON_SELECTOR = ", ".join(
    (
        'button[aria-label*="Send" i]:not([data-testid="stop-button"])',
        'button[aria-label*="Submit" i]:not([data-testid="stop-button"])',
        'button[data-testid*="send" i]:not([data-testid="stop-button"])',
    )
)
LIVE_SEND_BUTTON_BROAD_SELECTOR = ", ".join(
    (
        'form:has([contenteditable="true"]) button[type="submit"]',
        'form:has(textarea[name="prompt-textarea"]) button[type="submit"]',
        'form:has(textarea[placeholder*="message" i]) button[type="submit"]',
        'form:has(textarea[placeholder*="ask" i]) button[type="submit"]',
    )
)


def _is_page_navigate_timeout(exc: BaseException) -> bool:
    return isinstance(exc, TimeoutError) and "CDP timeout: Page.navigate" in str(exc)


def _is_expected_new_chat_url(url: str, gizmo_id: str | None) -> bool:
    """Require the actual destination, not merely any ChatGPT page with a composer."""
    if not isinstance(url, str) or not url:
        return False
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    if host not in {"chatgpt.com", "www.chatgpt.com"}:
        return False
    path = parsed.path.rstrip("/") or "/"
    if gizmo_id:
        return path == f"/g/{gizmo_id}/project"
    # A fresh non-project chat may normalize/drop ?model=auto, but it must not
    # still be an existing conversation or another project route.
    return path == "/"


async def _new_chat_is_ready(driver: Any, gizmo_id: str | None) -> bool:
    """Observe whether the requested new-chat destination is actually usable."""
    try:
        if not await driver._has_composer():
            return False
        url = await driver._js("location.href", timeout=5)
    except Exception:
        return False
    return _is_expected_new_chat_url(url, gizmo_id)


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


def _visible_composer_probe_js() -> str:
    """Return JS that accepts only a visible, writable prompt editor."""
    selectors = json.dumps([LIVE_COMPOSER_SELECTOR, LIVE_COMPOSER_FALLBACK_SELECTOR])
    return (
        "(function(){"
        f"  var selectors={selectors};"
        "  for (var s=0; s<selectors.length; s++) {"
        "    var nodes=document.querySelectorAll(selectors[s]);"
        "    for (var i=0; i<nodes.length; i++) {"
        "      var el=nodes[i];"
        "      var r=el.getBoundingClientRect();"
        "      var cs=getComputedStyle(el);"
        "      var enabled=!el.disabled && el.getAttribute('aria-disabled')!=='true';"
        "      var visible=r.width>20 && r.height>20 && cs.display!=='none' && "
        "          cs.visibility!=='hidden' && cs.opacity!=='0';"
        "      var editable=el.tagName==='TEXTAREA' || el.isContentEditable || "
        "          el.getAttribute('contenteditable')==='true';"
        "      if (visible && enabled && editable) {"
        "        return JSON.stringify({ready:true, tag:el.tagName, id:el.id||'', "
        "          role:el.getAttribute('role')||'', lexical:el.getAttribute('data-lexical-editor')||''});"
        "      }"
        "    }"
        "  }"
        "  return JSON.stringify({ready:false});"
        "})()"
    )


def install_runtime_hotfixes() -> None:
    """Install idempotent state-reconciliation guards on the production classes."""
    global _INSTALLED
    if _INSTALLED:
        return

    from aiohttp import web

    from . import cdp_driver as cdp_driver_module
    from . import chatgpt_dom as dom_module
    from .api_server import APIServer
    from .cdp_driver import CDPDriver, SendReadinessError
    from .chatgpt_dom import ChatGPTDom

    # Keep every code path on the same current selector set. cdp_driver imports
    # the constants into its own module namespace, while ChatGPTDom reads its
    # module globals at call time.
    dom_module.COMPOSER_SELECTOR = LIVE_COMPOSER_SELECTOR
    dom_module.COMPOSER_FALLBACK_SELECTOR = LIVE_COMPOSER_FALLBACK_SELECTOR
    dom_module.SEND_BUTTON_SELECTOR = LIVE_SEND_BUTTON_SELECTOR
    dom_module.SEND_BUTTON_BROAD_SELECTOR = LIVE_SEND_BUTTON_BROAD_SELECTOR
    cdp_driver_module.COMPOSER_SELECTOR = LIVE_COMPOSER_SELECTOR
    cdp_driver_module.COMPOSER_FALLBACK_SELECTOR = LIVE_COMPOSER_FALLBACK_SELECTOR
    cdp_driver_module.SEND_BUTTON_SELECTOR = LIVE_SEND_BUTTON_SELECTOR

    original_has_composer = ChatGPTDom._has_composer
    original_navigate_new_chat = CDPDriver.navigate_new_chat
    original_navigate_conversation = CDPDriver.navigate_conversation

    async def visible_has_composer(self) -> bool:
        """Reject hidden fallback editors and accept current visible editor variants."""
        try:
            raw = await self._driver._js(_visible_composer_probe_js())
            return bool(json.loads(raw).get("ready")) if raw else False
        except Exception:
            # Preserve the historical implementation as a last compatibility
            # fallback if the richer probe itself cannot execute.
            return await original_has_composer(self)

    async def navigate_new_chat_reconciled(self, gizmo_id: str | None = None) -> None:
        try:
            await original_navigate_new_chat(self, gizmo_id)
        except TimeoutError as exc:
            if not _is_page_navigate_timeout(exc):
                raise
            logger.warning(
                "Page.navigate timed out while opening a new chat; reconciling live page state"
            )
            if await _wait_until(
                lambda: _new_chat_is_ready(self, gizmo_id),
                NAVIGATION_RECONCILE_TIMEOUT_SECONDS,
            ):
                # The CDP command response was lost/late, but the browser did
                # complete the transition. Do not navigate again: a second
                # navigation can create another chat/tab and is unnecessary.
                self._current_conv_id = None
                logger.info("Recovered Page.navigate timeout: new-chat composer is ready")
                return
            raise

        # The upstream method historically fell through after its readiness
        # loop even if no composer was ever found. Make success explicit.
        if await _wait_until(
            lambda: _new_chat_is_ready(self, gizmo_id),
            POST_NAVIGATION_COMPOSER_TIMEOUT_SECONDS,
        ):
            self._current_conv_id = None
            return
        await self._capture_selector_diagnostic("composer (post navigate_new_chat)")
        raise SendReadinessError(
            "New-chat navigation completed, but no visible writable composer was found"
        )

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
            # CDP future behind. Let the single shared task finish in the
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

    ChatGPTDom._has_composer = visible_has_composer
    CDPDriver.navigate_new_chat = navigate_new_chat_reconciled
    CDPDriver.navigate_conversation = navigate_conversation_reconciled
    APIServer._handle_models = bounded_models
    _INSTALLED = True
