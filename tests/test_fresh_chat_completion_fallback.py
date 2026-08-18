"""Regression tests for fresh-chat completion after a missed identity capture."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from chatgpt_web2api.completion_detector import (
    CompletionDetector,
    _dom_completion_fallback_allowed,
)
from chatgpt_web2api.turn_anchor import TurnAnchor, TurnEndResult


def _anchor(*, mode="fresh_chat", captured_id=None):
    return TurnAnchor(
        sent_text="hello",
        mode=mode,
        captured_user_message_id=captured_id,
    )


def test_allows_dom_fallback_when_backend_is_unavailable():
    assert _dom_completion_fallback_allowed(
        conv_id_for_check="",
        backend_fetch_failed=False,
        backend_status=None,
        turn_anchor=_anchor(),
    )
    assert _dom_completion_fallback_allowed(
        conv_id_for_check="conv-1",
        backend_fetch_failed=True,
        backend_status="fetch_failed",
        turn_anchor=_anchor(),
    )


def test_allows_unanchored_fresh_chat_when_backend_is_not_authoritative():
    assert _dom_completion_fallback_allowed(
        conv_id_for_check="conv-1",
        backend_fetch_failed=False,
        backend_status="not_ready",
        turn_anchor=_anchor(),
    )


def test_keeps_existing_conversation_fail_closed():
    assert not _dom_completion_fallback_allowed(
        conv_id_for_check="conv-1",
        backend_fetch_failed=False,
        backend_status="not_ready",
        turn_anchor=_anchor(mode="existing_conversation"),
    )


def test_captured_fresh_chat_keeps_backend_authoritative():
    assert not _dom_completion_fallback_allowed(
        conv_id_for_check="conv-1",
        backend_fetch_failed=False,
        backend_status="not_ready",
        turn_anchor=_anchor(captured_id="user-msg-1"),
    )


def test_unanchored_fresh_chat_does_not_bypass_unchecked_backend():
    assert not _dom_completion_fallback_allowed(
        conv_id_for_check="conv-1",
        backend_fetch_failed=False,
        backend_status=None,
        turn_anchor=_anchor(),
    )


@pytest.mark.asyncio
async def test_detector_finishes_fresh_chat_when_backend_cannot_correlate_completed_dom_row():
    driver = MagicMock()
    driver._current_conv_id = None
    driver._get_live_conversation_id_best_effort = AsyncMock(return_value="conv-1")
    driver._fetch_end_turn_for_turn = AsyncMock(
        return_value=TurnEndResult(status="not_ready", diagnostic={"reason": "not_end_turn"})
    )

    poll = json.dumps(
        {
            "text": "Quick greeting",
            "md_text": "Quick greeting",
            "html_len": 120,
            "child_count": 1,
            "has_action": True,
            "is_thinking": False,
        }
    )

    async def fake_js(expr):
        if "document.body" in expr:
            return json.dumps({"text": ""})
        if "getBoundingClientRect" in expr:
            return poll
        return "1"  # Phase-1 assistant count: 0 -> 1.

    driver._js_strict = fake_js
    detector = CompletionDetector(driver)
    deltas = []
    async for chunk in detector.stream_until_complete(
        initial_count=0,
        timeout=5,
        turn_anchor=_anchor(),
    ):
        if chunk.delta:
            deltas.append(chunk.delta)

    assert "".join(deltas) == "Quick greeting"
    assert driver._get_live_conversation_id_best_effort.await_count == 1
    assert driver._fetch_end_turn_for_turn.await_count == 1
