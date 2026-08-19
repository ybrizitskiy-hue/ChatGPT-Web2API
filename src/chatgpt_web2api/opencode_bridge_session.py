"""OpenCode session affinity and native-tool isolation.

This layer sits entirely in the OpenCode sidecar. It keeps one growing OpenCode
message history attached to one ChatGPT web conversation, sends only the new
delta on continuation turns, answers OpenCode's title-generator request locally,
and prevents the web model from escaping into ChatGPT-native browser/connector
actions when OpenCode tools are available.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any

from aiohttp import web

from .opencode_bridge import ToolProtocolError, _CachedResult
from .opencode_bridge_agentic import AgenticOpenCodeBridge
from .opencode_bridge_runtime import (
    BRIDGE_APP_KEY,
    DEFAULT_CACHE_MAX_ENTRIES,
    DEFAULT_CACHE_TTL,
    DEFAULT_HEARTBEAT_INTERVAL,
    DEFAULT_REQUEST_TIMEOUT,
    DEFAULT_UPSTREAM,
    RuntimeOpenCodeBridge,
)

_CONTINUATION_MARKER = "_w2a_opencode_continuation"
_SESSION_TTL_SECONDS = 8 * 60 * 60
_SESSION_MAX_ENTRIES = 64
_TITLE_MARKERS = ("you are a title generator", "output only a thread title")
_GITHUB_RE = re.compile(r"\bgithub\b|github\.com", re.IGNORECASE)
_GITHUB_ACTION_RE = re.compile(
    r"\b(open|go|visit|check|inspect|look|review|read|fetch|search|find|list|clone|"
    r"pull|push|commit|merge|create|update|comment|close|reopen)\b|"
    r"(зайди|открой|проверь|посмотри|прочитай|найди|поищи|клонируй|создай|"
    r"обнови|закоммить|запуш|смерж|смёрж)",
    re.IGNORECASE,
)


@dataclass
class _ConversationState:
    conversation_id: str
    auth_scope: str
    history: tuple[str, ...]
    updated_at: float


class SessionOpenCodeBridge(AgenticOpenCodeBridge):
    """Agentic bridge with per-history ChatGPT conversation affinity."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._conversation_lock = asyncio.Lock()
        self._conversations: dict[str, _ConversationState] = {}

    async def close(self, app: web.Application) -> None:
        async with self._conversation_lock:
            self._conversations.clear()
        await super().close(app)

    @classmethod
    def _tool_instructions(cls, tools: list[dict[str, Any]], body: dict[str, Any]) -> str:
        return (
            super()._tool_instructions(tools, body)
            + "\n[Transport boundary]\n"
            "The ChatGPT web page is transport only. NEVER invoke ChatGPT-native web browsing, "
            "web search, connectors, plugins, actions, code interpreter, file tools, or GitHub "
            "authorization flows. Never trigger a permission/confirmation prompt in the ChatGPT "
            "browser. For GitHub, web, repository, filesystem, and shell work, use ONLY the "
            "OpenCode functions listed above (for example a dedicated GitHub/MCP tool, webfetch, "
            "or bash with git/gh when available). If no suitable OpenCode function is listed, "
            "say so instead of using a native ChatGPT capability."
        )

    @staticmethod
    def _continuation_has_user_followup(body: dict[str, Any]) -> bool:
        """Return True only for a real new user turn, never for a tool-result delta."""
        return any(
            isinstance(message, dict) and message.get("role") == "user"
            for message in body.get("messages") or []
        )

    @classmethod
    def _prepare_upstream_body(cls, body: dict[str, Any]) -> dict[str, Any]:
        """Send only continuation delta; refresh tool protocol on new user follow-ups.

        Tool-result turns stay minimal because the tool schema is already visible in
        the ChatGPT conversation. A later user follow-up may have a different set of
        OpenCode tools or a stronger ``tool_choice`` (notably GitHub requests forced
        to ``required``), so that turn re-advertises the current tool protocol without
        replaying the rest of OpenCode history or creating a new ChatGPT conversation.
        """
        if not body.get(_CONTINUATION_MARKER):
            return super()._prepare_upstream_body(body)

        upstream = dict(body)
        upstream["stream"] = False
        upstream["messages"] = cls._normalise_messages(body)
        tools = cls._function_tools(body.get("tools") or [])
        mode, named = cls._tool_choice(body)
        names = {(item.get("function") or {}).get("name") for item in tools}
        if mode in {"required", "named"} and not tools:
            raise ToolProtocolError(
                "A required tool_choice was supplied without a usable function tool",
                code="invalid_tool_choice",
                status=400,
            )
        if mode == "named" and named not in names:
            raise ToolProtocolError(
                f"Named tool_choice refers to unknown tool: {named}",
                code="invalid_tool_choice",
                status=400,
            )

        if tools and mode != "none" and cls._continuation_has_user_followup(body):
            upstream["messages"].insert(
                0,
                {
                    "role": "system",
                    "content": cls._tool_instructions(tools, body),
                },
            )

        upstream.pop("tools", None)
        upstream.pop("tool_choice", None)
        upstream.pop("parallel_tool_calls", None)
        upstream.pop("stream_options", None)
        upstream.pop(_CONTINUATION_MARKER, None)
        return upstream

    @classmethod
    def _last_non_system_message(cls, body: dict[str, Any]) -> dict[str, Any] | None:
        for message in reversed(body.get("messages") or []):
            if isinstance(message, dict) and message.get("role") != "system":
                return message
        return None

    @classmethod
    def _explicit_github_tool_intent(cls, body: dict[str, Any]) -> bool:
        if not cls._function_tools(body.get("tools") or []):
            return False
        last = cls._last_non_system_message(body)
        if not last or last.get("role") != "user":
            return False
        text = cls._content_text(last.get("content"))
        return bool(_GITHUB_RE.search(text) and _GITHUB_ACTION_RE.search(text))

    @classmethod
    def _force_opencode_tool_for_github(cls, body: dict[str, Any]) -> dict[str, Any]:
        """Force a first GitHub action through OpenCode rather than ChatGPT browsing."""
        if not cls._explicit_github_tool_intent(body):
            return body
        mode, _ = cls._tool_choice(body)
        if mode != "auto":
            return body
        forced = dict(body)
        forced["tool_choice"] = "required"
        return forced

    @classmethod
    def _is_title_request(cls, body: dict[str, Any]) -> bool:
        system = "\n".join(
            cls._content_text(message.get("content"))
            for message in body.get("messages") or []
            if isinstance(message, dict) and message.get("role") == "system"
        ).lower()
        return all(marker in system for marker in _TITLE_MARKERS)

    @classmethod
    def _local_title(cls, body: dict[str, Any]) -> str:
        text = cls._latest_user_text(body)
        if "[User]" in text:
            text = text.rsplit("[User]", 1)[-1]
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        candidate = lines[-1] if lines else "OpenCode session"
        candidate = re.sub(r"[`*_#]+", "", candidate)
        candidate = re.sub(r"\s+", " ", candidate).strip(" .:-")
        words = candidate.split()
        if len(words) > 8:
            candidate = " ".join(words[:8])
        if len(candidate) > 72:
            candidate = candidate[:72].rstrip()
        return candidate or "OpenCode session"

    @classmethod
    def _local_title_result(cls, body: dict[str, Any]) -> _CachedResult:
        payload = {
            "id": f"chatcmpl-local-title-{uuid.uuid4().hex[:12]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": body.get("model", "auto"),
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": cls._local_title(body)},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }
        return _CachedResult(
            expires_at=time.monotonic() + 60,
            status=200,
            headers={},
            payload=payload,
            cacheable=True,
        )

    @staticmethod
    def _auth_scope(auth_header: str | None) -> str:
        return hashlib.sha256((auth_header or "").encode("utf-8")).hexdigest()

    @staticmethod
    def _canonical_history(body: dict[str, Any]) -> tuple[str, ...]:
        return tuple(
            json.dumps(message, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            for message in body.get("messages") or []
            if isinstance(message, dict)
        )

    def _prune_conversations_locked(self) -> None:
        now = time.monotonic()
        stale = [
            conversation_id
            for conversation_id, state in self._conversations.items()
            if now - state.updated_at > _SESSION_TTL_SECONDS
        ]
        for conversation_id in stale:
            self._conversations.pop(conversation_id, None)
        while len(self._conversations) > _SESSION_MAX_ENTRIES:
            oldest = min(self._conversations.values(), key=lambda state: state.updated_at)
            self._conversations.pop(oldest.conversation_id, None)

    async def _bind_conversation(
        self,
        body: dict[str, Any],
        auth_header: str | None,
    ) -> tuple[dict[str, Any], tuple[str, ...]]:
        history = self._canonical_history(body)
        if body.get("conversation_id") or not history:
            return body, history

        auth_scope = self._auth_scope(auth_header)
        async with self._conversation_lock:
            self._prune_conversations_locked()
            candidates = [
                state
                for state in self._conversations.values()
                if state.auth_scope == auth_scope
                and len(state.history) < len(history)
                and history[: len(state.history)] == state.history
            ]
            if not candidates:
                return body, history
            longest = max(len(state.history) for state in candidates)
            candidates = [state for state in candidates if len(state.history) == longest]
            if len({state.conversation_id for state in candidates}) != 1:
                return body, history
            state = candidates[0]

        delta = [
            dict(message)
            for message in (body.get("messages") or [])[len(state.history) :]
            if isinstance(message, dict) and message.get("role") != "assistant"
        ]
        if not delta:
            return body, history
        bound = dict(body)
        bound["conversation_id"] = state.conversation_id
        bound["messages"] = delta
        bound[_CONTINUATION_MARKER] = True
        return bound, history

    async def _remember_conversation(
        self,
        history: tuple[str, ...],
        auth_header: str | None,
        result: _CachedResult,
    ) -> str | None:
        if result.status >= 400:
            return None
        conversation_id = result.payload.get("conversation_id")
        if not isinstance(conversation_id, str) or not conversation_id:
            return None
        async with self._conversation_lock:
            self._conversations[conversation_id] = _ConversationState(
                conversation_id=conversation_id,
                auth_scope=self._auth_scope(auth_header),
                history=history,
                updated_at=time.monotonic(),
            )
            self._prune_conversations_locked()
        return conversation_id

    @classmethod
    def _same_chat_environment_repair(
        cls,
        body: dict[str, Any],
        conversation_id: str | None,
    ) -> dict[str, Any]:
        if not conversation_id:
            return cls._repair_environment_body(body)
        repaired = dict(body)
        repaired["conversation_id"] = conversation_id
        repaired["messages"] = [
            {
                "role": "user",
                "content": (
                    "[OpenCode local-tool correction]\n"
                    "The OpenCode functions supplied to you are real capabilities. You MUST now "
                    "request exactly one appropriate available OpenCode tool. Do not use any "
                    "native ChatGPT browser, web, connector, plugin, action, file, code-interpreter, "
                    "or authorization capability. Reply only with the OpenCode tool-call JSON envelope."
                ),
            }
        ]
        repaired["tool_choice"] = "required"
        repaired[_CONTINUATION_MARKER] = True
        return repaired

    async def _do_upstream(
        self,
        body: dict[str, Any],
        auth_header: str | None,
    ) -> _CachedResult:
        if self._is_title_request(body):
            return self._local_title_result(body)

        policy_body = self._force_opencode_tool_for_github(body)
        bound_body, history = await self._bind_conversation(policy_body, auth_header)

        # Bypass AgenticOpenCodeBridge._do_upstream so its older denial retry
        # cannot create a fresh ChatGPT conversation. Runtime protocol repair
        # and error mapping are still preserved.
        result = await RuntimeOpenCodeBridge._do_upstream(self, bound_body, auth_header)
        conversation_id = await self._remember_conversation(history, auth_header, result)
        if not self._repairable_environment_denial(policy_body, result):
            return result

        repaired = self._same_chat_environment_repair(policy_body, conversation_id)
        repaired_result = await RuntimeOpenCodeBridge._do_upstream(self, repaired, auth_header)
        if conversation_id and repaired_result.status < 400:
            repaired_result.payload.setdefault("conversation_id", conversation_id)
        return repaired_result


def create_app(
    *,
    upstream: str | None = None,
    cache_ttl: float | None = None,
    request_timeout: float | None = None,
    cache_max_entries: int | None = None,
    heartbeat_interval: float | None = None,
    api_key: str | None = None,
) -> web.Application:
    bridge = SessionOpenCodeBridge(
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
