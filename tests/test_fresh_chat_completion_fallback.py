"""Regression tests for fresh-chat completion after a missed identity capture."""

from chatgpt_web2api.completion_detector import _dom_completion_fallback_allowed
from chatgpt_web2api.turn_anchor import TurnAnchor


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
