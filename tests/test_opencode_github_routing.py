from chatgpt_web2api.opencode_bridge_github_routing import GitHubRoutingOpenCodeBridge
from chatgpt_web2api.opencode_bridge_session import _CONTINUATION_MARKER


def _tool(name: str) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": f"{name} tool",
            "parameters": {"type": "object", "properties": {}},
        },
    }


def _body(text: str, names: list[str], tool_choice="auto") -> dict:
    return {
        "model": "auto",
        "messages": [{"role": "user", "content": text}],
        "tools": [_tool(name) for name in names],
        "tool_choice": tool_choice,
    }


def _choice_name(body: dict) -> str | None:
    choice = body.get("tool_choice")
    if not isinstance(choice, dict):
        return None
    return (choice.get("function") or {}).get("name")


def test_public_github_read_prefers_webfetch_over_bash() -> None:
    body = _body(
        "Inspect https://github.com/ybrizitskiy-hue/ChatGPT-Web2API and tell me the latest commit message.",
        ["bash", "websearch", "webfetch"],
    )

    routed = GitHubRoutingOpenCodeBridge._force_opencode_tool_for_github(body)

    assert _choice_name(routed) == "webfetch"


def test_public_github_read_falls_back_to_websearch_before_bash() -> None:
    body = _body(
        "Check https://github.com/example/project and list the latest release.",
        ["bash", "websearch"],
    )

    routed = GitHubRoutingOpenCodeBridge._force_opencode_tool_for_github(body)

    assert _choice_name(routed) == "websearch"


def test_github_write_does_not_force_webfetch() -> None:
    body = _body(
        "Merge the pull request at https://github.com/example/project/pull/42",
        ["bash", "webfetch"],
    )

    routed = GitHubRoutingOpenCodeBridge._force_opencode_tool_for_github(body)

    assert routed["tool_choice"] == "required"


def test_explicit_tool_choice_none_is_respected() -> None:
    body = _body(
        "Inspect https://github.com/example/project",
        ["bash", "webfetch"],
        tool_choice="none",
    )

    routed = GitHubRoutingOpenCodeBridge._force_opencode_tool_for_github(body)

    assert routed["tool_choice"] == "none"


def test_same_chat_github_followup_advertises_named_webfetch_policy() -> None:
    body = _body(
        "Inspect https://github.com/ybrizitskiy-hue/ChatGPT-Web2API and tell me the latest commit message.",
        ["bash", "webfetch"],
    )
    routed = GitHubRoutingOpenCodeBridge._force_opencode_tool_for_github(body)
    routed[_CONTINUATION_MARKER] = True
    routed["conversation_id"] = "conv-123"

    prepared = GitHubRoutingOpenCodeBridge._prepare_upstream_body(routed)

    assert prepared["conversation_id"] == "conv-123"
    assert "tools" not in prepared
    assert "tool_choice" not in prepared
    system = prepared["messages"][0]
    assert system["role"] == "system"
    assert "webfetch" in system["content"]
    assert "Do NOT choose bash+gh" in system["content"]
    assert "ChatGPT-native" in system["content"]
