"""Agentic resilience layer for the OpenCode compatibility bridge.

This module deliberately wraps only the OpenCode sidecar. It does not patch or
modify Web2API's Chrome/CDP transport. The extra behavior is limited to model
compliance when OpenCode has supplied local tools but the web model incorrectly
claims that the local filesystem or terminal is unavailable.
"""

from __future__ import annotations

import os
import re
from typing import Any

from aiohttp import web

from .opencode_bridge import _CachedResult
from .opencode_bridge_runtime import (
    BRIDGE_APP_KEY,
    DEFAULT_CACHE_MAX_ENTRIES,
    DEFAULT_CACHE_TTL,
    DEFAULT_HEARTBEAT_INTERVAL,
    DEFAULT_REQUEST_TIMEOUT,
    DEFAULT_UPSTREAM,
    RuntimeOpenCodeBridge,
)

_LOCAL_TOOL_NAMES = {
    "apply_patch",
    "bash",
    "edit",
    "glob",
    "grep",
    "read",
    "write",
}
_ACTION_RE = re.compile(
    r"\b(read|write|edit|modify|change|create|delete|remove|run|execute|inspect|"
    r"list|search|grep|patch|open)\b",
    re.IGNORECASE,
)
_LOCAL_OBJECT_RE = re.compile(
    r"\b(file|filesystem|directory|folder|terminal|shell|command|repo|repository|"
    r"code|readme)\b",
    re.IGNORECASE,
)
_DENIAL_MARKERS = (
    "can't access",
    "cannot access",
    "can't modify",
    "cannot modify",
    "unable to access",
    "unable to modify",
    "do not have access",
    "don't have access",
    "no access to",
    "can't interact with",
    "cannot interact with",
    "from this environment",
    "не могу получить доступ",
    "не могу изменить",
    "не имею доступа",
    "нет доступа",
)


class AgenticOpenCodeBridge(RuntimeOpenCodeBridge):
    """Runtime bridge with one bounded retry for false local-access denials."""

    @classmethod
    def _tool_instructions(cls, tools: list[dict[str, Any]], body: dict[str, Any]) -> str:
        base = super()._tool_instructions(tools, body)
        return (
            base
            + "\nThe listed tools are real OpenCode capabilities on the user's local machine. "
            "They are the mechanism by which you access files, repositories, and the terminal. "
            "When the user asks for a local action and a matching tool is available, request the "
            "tool instead of claiming that this ChatGPT environment cannot access the filesystem "
            "or terminal."
        )

    @staticmethod
    def _content_text(content: Any) -> str:
        if isinstance(content, str):
            return content
        if not isinstance(content, list):
            return ""
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
                continue
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            if isinstance(text, str):
                parts.append(text)
        return "\n".join(parts)

    @classmethod
    def _latest_user_text(cls, body: dict[str, Any]) -> str:
        for message in reversed(body.get("messages") or []):
            if isinstance(message, dict) and message.get("role") == "user":
                return cls._content_text(message.get("content"))
        return ""

    @classmethod
    def _explicit_local_tool_intent(cls, body: dict[str, Any]) -> bool:
        tools = cls._function_tools(body.get("tools") or [])
        if not tools:
            return False
        text = cls._latest_user_text(body).lower()
        if not text:
            return False
        names = {
            str((tool.get("function") or {}).get("name", "")).lower()
            for tool in tools
            if isinstance(tool, dict)
        }
        names.discard("")
        if any(name in text for name in names):
            return True
        if not (names & _LOCAL_TOOL_NAMES):
            return False
        return bool(_ACTION_RE.search(text) and _LOCAL_OBJECT_RE.search(text))

    @staticmethod
    def _looks_like_local_access_denial(text: str) -> bool:
        normalized = text.replace("’", "'").replace("‘", "'").lower()
        return any(marker in normalized for marker in _DENIAL_MARKERS)

    @classmethod
    def _repairable_environment_denial(
        cls,
        body: dict[str, Any],
        result: _CachedResult,
    ) -> bool:
        if result.status >= 400:
            return False
        mode, _ = cls._tool_choice(body)
        if mode == "none" or not cls._explicit_local_tool_intent(body):
            return False
        text = cls._message_text(result.payload)
        return isinstance(text, str) and cls._looks_like_local_access_denial(text)

    @classmethod
    def _repair_environment_body(cls, body: dict[str, Any]) -> dict[str, Any]:
        repaired = dict(body)
        messages = [dict(item) for item in body.get("messages") or []]
        repair = (
            "[OpenCode local-tool correction]\n"
            "Your previous response incorrectly claimed that the user's local filesystem or "
            "terminal was unavailable. The function tools supplied with this request are real "
            "OpenCode capabilities and can perform the requested local action. You MUST now "
            "request exactly one appropriate available tool. Do not claim that local access is "
            "unavailable. Reply only with the OpenCode tool-call JSON envelope."
        )
        system_index = next(
            (i for i, message in enumerate(messages) if message.get("role") == "system"),
            None,
        )
        if system_index is None:
            messages.insert(0, {"role": "system", "content": repair})
        else:
            messages[system_index]["content"] = (
                str(messages[system_index].get("content") or "") + "\n\n" + repair
            )
        repaired["messages"] = messages
        repaired["tool_choice"] = "required"
        return repaired

    async def _do_upstream(
        self,
        body: dict[str, Any],
        auth_header: str | None,
    ) -> _CachedResult:
        result = await super()._do_upstream(body, auth_header)
        if not self._repairable_environment_denial(body, result):
            return result
        return await super()._do_upstream(self._repair_environment_body(body), auth_header)


def create_app(
    *,
    upstream: str | None = None,
    cache_ttl: float | None = None,
    request_timeout: float | None = None,
    cache_max_entries: int | None = None,
    heartbeat_interval: float | None = None,
    api_key: str | None = None,
) -> web.Application:
    bridge = AgenticOpenCodeBridge(
        upstream=upstream or os.environ.get("W2A_UPSTREAM", DEFAULT_UPSTREAM),
        cache_ttl=(
            cache_ttl
            if cache_ttl is not None
            else float(os.environ.get("W2A_OPENCODE_CACHE_TTL", DEFAULT_CACHE_TTL))
        ),
        request_timeout=(
            request_timeout
            if request_timeout is not None
            else float(os.environ.get("W2A_OPENCODE_TIMEOUT", DEFAULT_REQUEST_TIMEOUT))
        ),
        cache_max_entries=(
            cache_max_entries
            if cache_max_entries is not None
            else int(os.environ.get("W2A_OPENCODE_CACHE_MAX_ENTRIES", DEFAULT_CACHE_MAX_ENTRIES))
        ),
        heartbeat_interval=(
            heartbeat_interval
            if heartbeat_interval is not None
            else float(
                os.environ.get("W2A_OPENCODE_HEARTBEAT_INTERVAL", DEFAULT_HEARTBEAT_INTERVAL)
            )
        ),
        api_key=api_key,
    )
    app = web.Application(client_max_size=10 * 1024 * 1024)
    app[BRIDGE_APP_KEY] = bridge
    app.on_startup.append(bridge.start)
    app.on_cleanup.append(bridge.close)
    app.router.add_get("/bridge/health", bridge.bridge_health)
    app.router.add_post("/v1/chat/completions", bridge.chat)
    app.router.add_post("/chat/completions", bridge.chat)
    app.router.add_get("/{tail:.*}", bridge.proxy_get)
    return app
