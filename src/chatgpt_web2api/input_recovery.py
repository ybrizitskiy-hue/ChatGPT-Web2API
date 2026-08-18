"""Recover ambiguous Input.insertText CDP timeouts without duplicating text."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import unicodedata
from typing import Any

from .runtime_hotfixes import LIVE_COMPOSER_FALLBACK_SELECTOR, LIVE_COMPOSER_SELECTOR

logger = logging.getLogger(__name__)

INSERT_TEXT_RECONCILE_TIMEOUT_SECONDS = 5.0
_INSTALLED = False


def _is_input_insert_timeout(exc: BaseException) -> bool:
    return isinstance(exc, TimeoutError) and "CDP timeout: Input.insertText" in str(exc)


def _canonical_text(value: str) -> str:
    return unicodedata.normalize(
        "NFC",
        value.replace("\r\n", "\n").replace("\r", "\n").replace("\u00a0", " "),
    )


def _visible_composer_text_probe_js() -> str:
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
        "      if (!visible || !enabled || !editable) continue;"
        "      var text=el.tagName==='TEXTAREA' ? (el.value || '') : (el.innerText || '');"
        "      return JSON.stringify({ready:true,text:text});"
        "    }"
        "  }"
        "  return JSON.stringify({ready:false,text:''});"
        "})()"
    )


async def _composer_text_matches(driver: Any, expected: str) -> bool:
    try:
        raw = await driver._js_strict(_visible_composer_text_probe_js(), timeout=5)
        data = json.loads(raw) if raw else {}
    except Exception:
        return False
    if not data.get("ready"):
        return False
    actual = data.get("text")
    if not isinstance(actual, str):
        return False
    canon_actual = _canonical_text(actual)
    canon_expected = _canonical_text(expected)
    return canon_actual == canon_expected or canon_actual == canon_expected + "\n"


async def _wait_until(predicate, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await predicate():
            return True
        await asyncio.sleep(0.2)
    return False


def install_input_recovery() -> None:
    """Install exact-state reconciliation for ambiguous Input.insertText timeouts."""
    global _INSTALLED
    if _INSTALLED:
        return

    from .chatgpt_dom import ChatGPTDom

    original_type_message = ChatGPTDom.type_message

    async def type_message_reconciled(self, text: str) -> None:
        try:
            await original_type_message(self, text)
            return
        except TimeoutError as exc:
            if not _is_input_insert_timeout(exc):
                raise
            logger.warning(
                "Input.insertText timed out; reconciling the live composer before deciding failure"
            )
            if await _wait_until(
                lambda: _composer_text_matches(self._driver, text),
                INSERT_TEXT_RECONCILE_TIMEOUT_SECONDS,
            ):
                logger.info(
                    "Recovered Input.insertText timeout: composer contains the exact intended text"
                )
                return
            raise

    ChatGPTDom.type_message = type_message_reconciled
    _INSTALLED = True
