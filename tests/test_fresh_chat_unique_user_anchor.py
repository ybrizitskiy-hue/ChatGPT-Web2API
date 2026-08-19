"""Regression tests for OpenCode fresh-chat correlation after UUID capture misses."""

from chatgpt_web2api.turn_anchor import (
    TurnAnchor,
    collapse_to_end_turn_status,
    select_end_turn_for_turn,
    select_text_for_turn,
)


def _user(node_id: str, text: str, *, children=None):
    return {
        "id": node_id,
        "role": "user",
        "text": text,
        "create_time": 100.0,
        "children": children or [],
    }


def _assistant(node_id: str, text: str, *, parent: str, end_turn: bool = True):
    return {
        "id": node_id,
        "role": "assistant",
        "text": text,
        "content_type": "text",
        "create_time": 101.0,
        "end_turn": end_turn,
        "parent": parent,
        "children": [],
    }


def test_fresh_chat_sole_user_correlates_when_backend_rewrites_prompt_text():
    mapping = {
        "nodes": {
            "u-1": _user("u-1", "backend-normalized prompt", children=["a-1"]),
            "a-1": _assistant("a-1", "Quick greeting", parent="u-1"),
        }
    }
    anchor = TurnAnchor(
        sent_text="[System Instructions]\nOpenCode original prompt",
        mode="fresh_chat",
    )

    text_result = select_text_for_turn(mapping, anchor)
    assert text_result.status == "matched"
    assert text_result.text == "Quick greeting"
    assert text_result.diagnostic["user_node"] == "u-1"

    end_result = select_end_turn_for_turn(mapping, anchor, had_non_text_content=False)
    assert end_result.status == "matched"
    assert collapse_to_end_turn_status(end_result) == "complete"


def test_fresh_chat_multiple_unmatched_user_nodes_stays_fail_closed():
    mapping = {
        "nodes": {
            "u-1": _user("u-1", "one"),
            "u-2": _user("u-2", "two"),
        }
    }
    anchor = TurnAnchor(sent_text="different OpenCode prompt", mode="fresh_chat")

    result = select_text_for_turn(mapping, anchor)
    assert result.status == "ambiguous"


def test_existing_conversation_does_not_use_unique_user_shortcut():
    mapping = {
        "nodes": {
            "u-1": _user("u-1", "backend-normalized prompt", children=["a-1"]),
            "a-1": _assistant("a-1", "Old reply", parent="u-1"),
        }
    }
    anchor = TurnAnchor(
        sent_text="different OpenCode prompt",
        mode="existing_conversation",
        latest_user_node_id="u-old",
        latest_user_create_time=99.0,
    )

    result = select_text_for_turn(mapping, anchor)
    assert result.status == "not_ready"
