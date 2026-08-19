"""Exact OpenCode session affinity via the session headers OpenCode already sends."""

from __future__ import annotations

import contextvars
import time
from dataclasses import dataclass
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
from .opencode_bridge_session import SessionOpenCodeBridge, _CONTINUATION_MARKER

_SESSION_CONTEXT: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "w2a_opencode_session_id", default=None
)
_EXACT_SESSION_TTL_SECONDS = 8 * 60 * 60
_EXACT_SESSION_MAX_ENTRIES = 64


@dataclass
class _ExactSessionState:
    conversation_id: str
    history: tuple[str, ...]
    updated_at: float


class ExactSessionOpenCodeBridge(SessionOpenCodeBridge):
    """Use OpenCode's X-Session-Id as the authoritative conversation key."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._exact_sessions: dict[tuple[str, str], _ExactSessionState] = {}

    async def close(self, app: web.Application) -> None:
        async with self._conversation_lock:
            self._exact_sessions.clear()
        await super().close(app)

    @staticmethod
    def _fingerprint(
        body: dict[str, Any],
        auth_header: str | None = None,
        idempotency_key: str | None = None,
    ) -> str:
        session_id = _SESSION_CONTEXT.get()
        if session_id:
            affinity = f"opencode-session:{session_id}"
            idempotency_key = f"{idempotency_key}|{affinity}" if idempotency_key else affinity
        return RuntimeOpenCodeBridge._fingerprint(body, auth_header, idempotency_key)

    def _prune_exact_sessions_locked(self) -> None:
        now = time.monotonic()
        stale = [
            key
            for key, state in self._exact_sessions.items()
            if now - state.updated_at > _EXACT_SESSION_TTL_SECONDS
        ]
        for key in stale:
            self._exact_sessions.pop(key, None)
        while len(self._exact_sessions) > _EXACT_SESSION_MAX_ENTRIES:
            oldest_key = min(
                self._exact_sessions,
                key=lambda key: self._exact_sessions[key].updated_at,
            )
            self._exact_sessions.pop(oldest_key, None)

    async def _bind_conversation(
        self,
        body: dict[str, Any],
        auth_header: str | None,
    ) -> tuple[dict[str, Any], tuple[str, ...]]:
        session_id = _SESSION_CONTEXT.get()
        if not session_id:
            return await super()._bind_conversation(body, auth_header)

        history = self._canonical_history(body)
        if body.get("conversation_id") or not history:
            return body, history

        key = (self._auth_scope(auth_header), session_id)
        async with self._conversation_lock:
            self._prune_exact_sessions_locked()
            state = self._exact_sessions.get(key)
        if state is None:
            return body, history

        messages = [item for item in body.get("messages") or [] if isinstance(item, dict)]
        if len(state.history) < len(history) and history[: len(state.history)] == state.history:
            source_delta = messages[len(state.history) :]
        else:
            # OpenCode can compact/rewrite visible history while keeping the same
            # session id. The header is authoritative; send only the newest user
            # or tool stimulus instead of replaying the compacted history.
            source_delta = []
            for message in reversed(messages):
                if message.get("role") in {"user", "tool"}:
                    source_delta = [message]
                    break

        delta = [dict(message) for message in source_delta if message.get("role") != "assistant"]
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
        conversation_id = await super()._remember_conversation(history, auth_header, result)
        session_id = _SESSION_CONTEXT.get()
        if not conversation_id or not session_id:
            return conversation_id
        key = (self._auth_scope(auth_header), session_id)
        async with self._conversation_lock:
            self._exact_sessions[key] = _ExactSessionState(
                conversation_id=conversation_id,
                history=history,
                updated_at=time.monotonic(),
            )
            self._prune_exact_sessions_locked()
        return conversation_id


def _session_id_from_headers(request: web.Request) -> str | None:
    return (
        request.headers.get("X-Session-Id")
        or request.headers.get("x-session-affinity")
        or request.headers.get("x-opencode-session")
    )


def create_app(
    *,
    upstream: str | None = None,
    cache_ttl: float | None = None,
    request_timeout: float | None = None,
    cache_max_entries: int | None = None,
    heartbeat_interval: float | None = None,
    api_key: str | None = None,
) -> web.Application:
    bridge = ExactSessionOpenCodeBridge(
        upstream=upstream or DEFAULT_UPSTREAM,
        cache_ttl=cache_ttl if cache_ttl is not None else DEFAULT_CACHE_TTL,
        request_timeout=(
            request_timeout if request_timeout is not None else DEFAULT_REQUEST_TIMEOUT
        ),
        cache_max_entries=(
            cache_max_entries if cache_max_entries is not None else DEFAULT_CACHE_MAX_ENTRIES
        ),
        heartbeat_interval=(
            heartbeat_interval
            if heartbeat_interval is not None
            else DEFAULT_HEARTBEAT_INTERVAL
        ),
        api_key=api_key,
    )

    @web.middleware
    async def opencode_session_middleware(
        request: web.Request, handler: Any
    ) -> web.StreamResponse:
        token = _SESSION_CONTEXT.set(_session_id_from_headers(request))
        try:
            return await handler(request)
        finally:
            _SESSION_CONTEXT.reset(token)

    app = web.Application(
        client_max_size=10 * 1024 * 1024,
        middlewares=[opencode_session_middleware],
    )
    app[BRIDGE_APP_KEY] = bridge
    app.on_startup.append(bridge.start)
    app.on_cleanup.append(bridge.close)
    app.router.add_get("/bridge/health", bridge.bridge_health)
    app.router.add_post("/v1/chat/completions", bridge.chat)
    app.router.add_post("/chat/completions", bridge.chat)
    app.router.add_get("/{tail:.*}", bridge.proxy_get)
    return app
