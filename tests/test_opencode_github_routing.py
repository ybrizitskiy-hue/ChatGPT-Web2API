import json

from chatgpt_web2api.opencode_bridge_github_routing import GitHubRoutingOpenCodeBridge
from chatgpt_web2api.opencode_bridge_session import _CONTINUATION_MARKER


def _tool(name: str) -> dict:
    properties = {}
    required = []
    if name == "webfetch":
        properties = {
            "url": {"type": "string"},
            "format": {"type": "string", "enum": ["text", "markdown", "html"]},
        }
        required = ["url"]
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": f"{name} tool",
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
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


def test_latest_commit_read_synthesizes_webfetch_to_github_api() -> None:
    body = _body(
        "Inspect https://github.com/ybrizitskiy-hue/ChatGPT-Web2API and tell me the latest commit message.",
        ["bash", "webfetch"],
    )

    plan = GitHubRoutingOpenCodeBridge._synthetic_public_github_tool_call(body)

    assert plan is not None
    name, arguments = plan
    assert name == "webfetch"
    assert arguments == {
        "url": "https://api.github.com/repos/ybrizitskiy-hue/ChatGPT-Web2API/commits?per_page=1",
        "format": "text",
    }


def test_generic_public_github_read_synthesizes_original_url() -> None:
    body = _body(
        "Read https://github.com/example/project/blob/main/README.md and summarize it.",
        ["bash", "webfetch"],
    )

    plan = GitHubRoutingOpenCodeBridge._synthetic_public_github_tool_call(body)

    assert plan == (
        "webfetch",
        {
            "url": "https://github.com/example/project/blob/main/README.md",
            "format": "markdown",
        },
    )


def test_tool_result_turn_never_resynthesizes_webfetch() -> None:
    body = _body(
        "Inspect https://github.com/example/project and tell me the latest commit message.",
        ["bash", "webfetch"],
    )
    body["messages"].extend(
        [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_webfetch",
                        "type": "function",
                        "function": {
                            "name": "webfetch",
                            "arguments": '{"url":"https://api.github.com/repos/example/project/commits?per_page=1","format":"text"}',
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_webfetch",
                "content": '[{"commit":{"message":"latest message"}}]',
            },
        ]
    )

    assert GitHubRoutingOpenCodeBridge._synthetic_public_github_tool_call(body) is None


def test_github_write_never_synthesizes_read_tool() -> None:
    body = _body(
        "Merge https://github.com/example/project/pull/42",
        ["bash", "webfetch"],
    )

    assert GitHubRoutingOpenCodeBridge._synthetic_public_github_tool_call(body) is None


def test_synthetic_payload_is_standard_openai_tool_call() -> None:
    body = _body(
        "Inspect https://github.com/example/project",
        ["webfetch"],
    )
    payload = GitHubRoutingOpenCodeBridge._synthetic_tool_payload(
        body,
        "webfetch",
        {"url": "https://github.com/example/project", "format": "markdown"},
    )

    choice = payload["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    call = choice["message"]["tool_calls"][0]
    assert call["type"] == "function"
    assert call["function"]["name"] == "webfetch"
    assert json.loads(call["function"]["arguments"]) == {
        "url": "https://github.com/example/project",
        "format": "markdown",
    }
