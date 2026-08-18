"""Hardened HTTP runtime for the OpenCode bridge.

The protocol translation lives in :mod:`opencode_bridge`. This module wraps
that implementation with strict request validation, upstream error mapping,
query-preserving GET proxying, explicit health, and shutdown cleanup.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any

from aiohttp import ClientError, ClientSession, ClientTimeout, ContentTypeError, web

from .opencode_bridge import OpenCodeBridge, _CachedResult

DEFAULT_UPSTREAM = "http://127.0.0.1:8080"
DEFAULT_CACHE_TTL = 60.0
DEFAULT_REQUEST_TIMEOUT = 930.0


class RuntimeOpenCodeBridge(OpenCodeBridge):
    """OpenCodeBridge with production-facing HTTP and lifecycle behaviour."""

    def __init__(
        self,
        upstream: str = DEFAULT_UPSTREAM,
        cache_ttl: float = DEFAULT_CACHE_TTL,
        request_timeout: float = DEFAULT_REQUEST_TIMEOUT,
    ) -> None:
        super().__init__(upstream=upstream, cache_ttl=max(0.0, float(cache_ttl)))
        self.request_timeout = max(1.0, float(request_timeout))

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

    async def _do_upstream(
        self,
        body: dict[str, Any],
        auth_header: str | None,
    ) -> _CachedResult:
        try:
            return await super()._do_upstream(body, auth_header)
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

    async def _finalize_task(self, key: str, task: asyncio.Task[_CachedResult]) -> None:
        """Promote only successful logical turns into the replay cache."""
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

    async def _get_result(
        self,
        key: str,
        body: dict[str, Any],
        auth_header: str | None,
    ) -> _CachedResult:
        """Join live turns, replay successes, and immediately retry completed errors.

        The base implementation removes a finished task through an asynchronous
        done-callback. A fast retry can arrive in the short interval after a
        non-cacheable task finished but before that callback removed it, causing
        the retry to reuse the just-finished 4xx/5xx. Detect that state under the
        lock and start a fresh upstream attempt instead.
        """
        now = time.monotonic()
        async with self._lock:
            stale = [cache_key for cache_key, value in self._cache.items() if value.expires_at <= now]
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
                        return completed
                task = None

            if task is None:
                task = asyncio.create_task(self._do_upstream(body, auth_header))
                self._inflight[key] = task
                task.add_done_callback(lambda done, k=key: self._schedule_finalize(k, done))

        return await asyncio.shield(task)

    async def chat(self, request: web.Request) -> web.StreamResponse:
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
            key = self._fingerprint(body, auth_header, idempotency_key)
            result = await self._get_result(key, body, auth_header)
        except asyncio.CancelledError:
            raise
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

        if body.get("stream") and result.status < 400:
            return web.Response(
                body=self._as_sse(result.payload),
                status=result.status,
                headers={**result.headers, "Cache-Control": "no-cache"},
                content_type="text/event-stream",
            )
        return web.json_response(result.payload, status=result.status, headers=result.headers)

    async def bridge_health(self, _request: web.Request) -> web.Response:
        return web.json_response(
            {
                "status": "healthy",
                "upstream": self.upstream,
                "inflight": len(self._inflight),
                "replay_cache": len(self._cache),
            }
        )

    async def proxy_get(self, request: web.Request) -> web.Response:
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
