"""Hardened HTTP runtime for the OpenCode bridge.

The protocol translation lives in :mod:`opencode_bridge`. This module wraps
that implementation with strict request validation, upstream error mapping,
query-preserving GET proxying, explicit health, bounded replay state, local
authentication, one-shot tool-protocol repair, and disconnect-safe SSE
keepalives.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import time
from typing import Any

from aiohttp import ClientError, ClientSession, ClientTimeout, ContentTypeError, web

from .opencode_bridge import OpenCodeBridge, ToolProtocolError, _CachedResult, _TOOL_SENTINEL

DEFAULT_UPSTREAM = "http://127.0.0.1:8080"
DEFAULT_CACHE_TTL = 60.0
DEFAULT_CACHE_MAX_ENTRIES = 256
DEFAULT_REQUEST_TIMEOUT = 930.0
DEFAULT_HEARTBEAT_INTERVAL = 10.0


class RuntimeOpenCodeBridge(OpenCodeBridge):
    """OpenCodeBridge with production-facing HTTP and lifecycle behaviour."""

    def __init__(
        self,
        upstream: str = DEFAULT_UPSTREAM,
        cache_ttl: float = DEFAULT_CACHE_TTL,
        request_timeout: float = DEFAULT_REQUEST_TIMEOUT,
        *,
        cache_max_entries: int = DEFAULT_CACHE_MAX_ENTRIES,
        heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL,
        api_key: str | None = None,
    ) -> None:
        super().__init__(upstream=upstream, cache_ttl=max(0.0, float(cache_ttl)))
        self.request_timeout = max(1.0, float(request_timeout))
        self.cache_max_entries = max(1, int(cache_max_entries))
        self.heartbeat_interval = max(0.1, float(heartbeat_interval))
        self.api_key = api_key

    async def start(self, _app: web.Application) -> None:
        self._session = ClientSession(timeout=ClientTimeout(total=self.request_timeout))

    async def close(self, _app: web.Application) -> None:
        tasks = list(set(self._inflight.values()))
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._inflight.clear()
        self._cache.clear()
        if self._session is not None:
            await self._session.close()
            self._session = None

    @staticmethod
    def _fingerprint(
        body: dict[str, Any],
        auth_header: str | None = None,
        idempotency_key: str | None = None,
    ) -> str:
        logical = dict(body)
        if idempotency_key:
            logical["_w2a_idempotency_key"] = idempotency_key
        return OpenCodeBridge._fingerprint(logical, auth_header)

    def _auth_error(self, request: web.Request) -> web.Response | None:
        """Validate the setup-generated bridge key before proxying account access."""
        if not self.api_key:
            return None
        auth = request.headers.get("Authorization", "")
        supplied = auth[7:] if auth.startswith("Bearer ") else ""
        if not supplied or not hmac.compare_digest(supplied, self.api_key):
            return web.json_response(
                {
                    "error": {
                        "message": "Invalid bridge API key",
                        "type": "auth_error",
                        "code": "invalid_api_key",
                    }
                },
                status=401,
            )
        return None

    def _upstream_error(self, message: str, code: str, status: int) -> _CachedResult:
        return _CachedResult(
            expires_at=time.monotonic(),
            status=status,
            headers={},
            payload={
                "error": {
                    "message": message,
                    "type": "server_error",
                    "code": code,
                }
            },
            cacheable=False,
        )

    @classmethod
    def _parse_tool_call(cls, text: str) -> tuple[str, dict[str, Any]] | None:
        """Accept the strict envelope plus harmless ``\"true\"`` sentinel drift."""
        parsed = super()._parse_tool_call(text)
        if parsed is not None:
            return parsed
        extracted = cls._extract_sentinel_object(text.strip())
        if not extracted:
            return None
        try:
            data = json.loads(extracted)
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(data, dict):
            return None
        sentinel = data.get(_TOOL_SENTINEL)
        if not (isinstance(sentinel, str) and sentinel.strip().lower() == "true"):
            return None
        name = data.get("name")
        arguments = data.get("arguments", {})
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                return None
        if not isinstance(name, str) or not name or not isinstance(arguments, dict):
            return None
        return name, arguments

    @staticmethod
    def _message_text(payload: dict[str, Any]) -> str | None:
        choices = payload.get("choices") or []
        if not choices or not isinstance(choices[0], dict):
            return None
        message = choices[0].get("message") or {}
        if not isinstance(message, dict):
            return None
        content = message.get("content")
        return content if isinstance(content, str) else None

    @staticmethod
    def _error_code(result: _CachedResult) -> str | None:
        error = result.payload.get("error")
        return error.get("code") if isinstance(error, dict) else None

    @classmethod
    def _repairable_tool_failure(cls, body: dict[str, Any], result: _CachedResult) -> bool:
        tools = cls._function_tools(body.get("tools") or [])
        mode, _ = cls._tool_choice(body)
        if not tools or mode == "none":
            return False
        if result.status >= 500 and cls._error_code(result) in {
            "invalid_tool_call",
            "unknown_tool_call",
            "wrong_tool_call",
            "tool_call_required",
        }:
            return True
        text = cls._message_text(result.payload)
        return result.status < 400 and isinstance(text, str) and _TOOL_SENTINEL in text

    @classmethod
    def _repair_body(cls, body: dict[str, Any]) -> dict[str, Any]:
        repaired = dict(body)
        messages = [dict(item) for item in body.get("messages") or []]
        repair = (
            "[OpenCode tool protocol repair]\n"
            "The previous attempt did not satisfy the tool-call wire format. "
            "For this retry, if a tool is requested by tool_choice or is needed, reply with ONLY "
            f'{{"{_TOOL_SENTINEL}":true,"name":"exact_available_tool_name","arguments":{{...}}}}. '
            "Use JSON boolean true (not a quoted string), an exact available tool name, and a JSON "
            "object for arguments. Do not add prose or markdown fences."
        )
        system_index = next(
            (i for i, msg in enumerate(messages) if msg.get("role") == "system"), None
        )
        if system_index is None:
            messages.insert(0, {"role": "system", "content": repair})
        else:
            messages[system_index]["content"] = (
                str(messages[system_index].get("content") or "") + "\n\n" + repair
            )
        repaired["messages"] = messages
        return repaired

    async def _do_upstream(
        self,
        body: dict[str, Any],
        auth_header: str | None,
    ) -> _CachedResult:
        try:
            result = await super()._do_upstream(body, auth_header)
            if self._repairable_tool_failure(body, result):
                result = await super()._do_upstream(self._repair_body(body), auth_header)
                if self._repairable_tool_failure(body, result):
                    return self._upstream_error(
                        "Model did not produce a valid tool call after one correction attempt",
                        "tool_protocol_correction_failed",
                        502,
                    )
            return result
        except asyncio.TimeoutError:
            return self._upstream_error(
                f"Web2API upstream timed out after {self.request_timeout:.0f}s",
                "upstream_timeout",
                504,
            )
        except ClientError as exc:
            return self._upstream_error(
                f"Web2API upstream connection failed: {exc}",
                "upstream_connection_error",
                502,
            )

    def _trim_cache_locked(self) -> None:
        while len(self._cache) > self.cache_max_entries:
            self._cache.pop(next(iter(self._cache)), None)

    async def _finalize_task(self, key: str, task: asyncio.Task[_CachedResult]) -> None:
        """Promote only successful logical turns into the bounded replay cache."""
        async with self._lock:
            if self._inflight.get(key) is not task:
                return
            self._inflight.pop(key, None)
            if task.cancelled():
                return
            try:
                result = task.result()
            except BaseException:
                return
            if result.cacheable and result.status < 400:
                self._cache[key] = result
                self._trim_cache_locked()

    async def _get_result(
        self,
        key: str,
        body: dict[str, Any],
        auth_header: str | None,
    ) -> _CachedResult:
        """Join live turns, replay successes, and immediately retry completed errors."""
        now = time.monotonic()
        async with self._lock:
            stale = [
                cache_key for cache_key, value in self._cache.items() if value.expires_at <= now
            ]
            for cache_key in stale:
                self._cache.pop(cache_key, None)

            cached = self._cache.get(key)
            if cached is not None:
                return cached

            task = self._inflight.get(key)
            if task is not None and task.done():
                self._inflight.pop(key, None)
                if not task.cancelled():
                    try:
                        completed = task.result()
                    except BaseException:
                        completed = None
                    if completed is not None and completed.cacheable and completed.status < 400:
                        self._cache[key] = completed
                        self._trim_cache_locked()
                        return completed
                task = None

            if task is None:
                task = asyncio.create_task(self._do_upstream(body, auth_header))
                self._inflight[key] = task
                task.add_done_callback(lambda done, k=key: self._schedule_finalize(k, done))

        return await asyncio.shield(task)

    @classmethod
    def _as_sse(cls, payload: dict[str, Any]) -> bytes:
        """Prefix the base SSE sequence with the standard assistant-role delta."""
        choices = payload.get("choices") or []
        if not choices:
            return OpenCodeBridge._as_sse(payload)
        role_event = {
            "id": payload.get("id", f"chatcmpl_{int(time.time() * 1000)}"),
            "object": "chat.completion.chunk",
            "created": payload.get("created", int(time.time())),
            "model": payload.get("model", ""),
            "choices": [
                {"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}
            ],
        }
        prefix = (
            "data: "
            + json.dumps(role_event, ensure_ascii=False, separators=(",", ":"))
            + "\n\n"
        ).encode("utf-8")
        return prefix + OpenCodeBridge._as_sse(payload)

    async def _stream_chat_result(
        self,
        request: web.Request,
        waiter: asyncio.Task[_CachedResult],
    ) -> web.StreamResponse:
        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
        await response.prepare(request)
        await response.write(b": w2a-connected\n\n")
        try:
            while not waiter.done():
                try:
                    await asyncio.wait_for(
                        asyncio.shield(waiter), timeout=self.heartbeat_interval
                    )
                except asyncio.TimeoutError:
                    await response.write(b": w2a-keepalive\n\n")

            result = await waiter
            if result.status < 400:
                await response.write(self._as_sse(result.payload))
            else:
                await response.write(
                    (
                        "data: "
                        + json.dumps(result.payload, ensure_ascii=False, separators=(",", ":"))
                        + "\n\ndata: [DONE]\n\n"
                    ).encode("utf-8")
                )
            await response.write_eof()
            return response
        except (ConnectionResetError, BrokenPipeError):
            # _get_result shields the shared upstream task, so cancelling this
            # waiter does not cancel the logical ChatGPT turn.
            waiter.cancel()
            return response
        except asyncio.CancelledError:
            waiter.cancel()
            raise

    async def chat(self, request: web.Request) -> web.StreamResponse:
        if auth_error := self._auth_error(request):
            return auth_error
        try:
            body = await request.json()
        except (json.JSONDecodeError, ContentTypeError, UnicodeDecodeError):
            return web.json_response(
                {
                    "error": {
                        "message": "Invalid JSON request body",
                        "type": "invalid_request_error",
                        "code": "invalid_json",
                    }
                },
                status=400,
            )
        if not isinstance(body, dict):
            return web.json_response(
                {
                    "error": {
                        "message": "Request body must be a JSON object",
                        "type": "invalid_request_error",
                        "code": "invalid_request",
                    }
                },
                status=400,
            )

        auth_header = request.headers.get("Authorization")
        idempotency_key = request.headers.get("Idempotency-Key") or request.headers.get(
            "X-Idempotency-Key"
        )
        try:
            # Preserve HTTP 400 for deterministic client mistakes before an SSE
            # response is prepared.
            self._prepare_upstream_body(body)
            key = self._fingerprint(body, auth_header, idempotency_key)
            if body.get("stream"):
                waiter = asyncio.create_task(self._get_result(key, body, auth_header))
                return await self._stream_chat_result(request, waiter)
            result = await self._get_result(key, body, auth_header)
        except asyncio.CancelledError:
            raise
        except ToolProtocolError as exc:
            return web.json_response(
                self._protocol_error_result(exc, 0).payload,
                status=exc.status,
            )
        except (TypeError, ValueError) as exc:
            return web.json_response(
                {
                    "error": {
                        "message": f"Invalid request: {exc}",
                        "type": "invalid_request_error",
                        "code": "invalid_request",
                    }
                },
                status=400,
            )
        except Exception as exc:
            return web.json_response(
                {
                    "error": {
                        "message": f"Bridge request failed: {exc}",
                        "type": "server_error",
                        "code": "bridge_error",
                    }
                },
                status=502,
            )

        return web.json_response(result.payload, status=result.status, headers=result.headers)

    async def bridge_health(self, request: web.Request) -> web.Response:
        if auth_error := self._auth_error(request):
            return auth_error
        return web.json_response(
            {
                "status": "healthy",
                "upstream": self.upstream,
                "inflight": len(self._inflight),
                "replay_cache": len(self._cache),
                "replay_cache_limit": self.cache_max_entries,
            }
        )

    async def proxy_get(self, request: web.Request) -> web.Response:
        if auth_error := self._auth_error(request):
            return auth_error
        assert self._session is not None
        path = request.match_info["tail"]
        target = f"{self.upstream}/{path}"
        if request.query_string:
            target += f"?{request.query_string}"
        headers: dict[str, str] = {}
        if auth := request.headers.get("Authorization"):
            headers["Authorization"] = auth
        try:
            async with self._session.get(target, headers=headers) as response:
                raw = await response.read()
                forwarded = {
                    name: response.headers[name]
                    for name in ("Content-Type", "Retry-After", "Cache-Control")
                    if name in response.headers
                }
                return web.Response(body=raw, status=response.status, headers=forwarded)
        except asyncio.TimeoutError:
            return web.json_response(
                {
                    "error": {
                        "message": "Web2API upstream GET timed out",
                        "type": "server_error",
                        "code": "upstream_timeout",
                    }
                },
                status=504,
            )
        except ClientError as exc:
            return web.json_response(
                {
                    "error": {
                        "message": f"Web2API upstream GET failed: {exc}",
                        "type": "server_error",
                        "code": "upstream_connection_error",
                    }
                },
                status=502,
            )


BRIDGE_APP_KEY = web.AppKey("opencode_bridge", RuntimeOpenCodeBridge)


def create_app(
    *,
    upstream: str | None = None,
    cache_ttl: float | None = None,
    request_timeout: float | None = None,
    cache_max_entries: int | None = None,
    heartbeat_interval: float | None = None,
    api_key: str | None = None,
) -> web.Application:
    bridge = RuntimeOpenCodeBridge(
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
            else int(
                os.environ.get("W2A_OPENCODE_CACHE_MAX_ENTRIES", DEFAULT_CACHE_MAX_ENTRIES)
            )
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