"""OpenCode compatibility bridge for ChatGPT-Web2API.

This is a thin OpenAI-compatible proxy that adds two behaviours the browser
bridge intentionally does not provide itself:

* translate OpenAI ``tools`` into a strict text protocol the ChatGPT web model
  can follow, then translate the model's JSON envelope back into OpenAI
  ``tool_calls``;
* coalesce/replay identical in-flight requests for a short period, so a client
  reconnect does not automatically create a second ChatGPT turn.

The upstream Web2API server remains untouched.  Run it normally, then point
this bridge at it with ``W2A_UPSTREAM`` (default http://127.0.0.1:8000).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
import uuid
from dataclasses import dataclass
from typing import Any

from aiohttp import ClientSession, ClientTimeout, web

_UPSTREAM = os.environ.get("W2A_UPSTREAM", "http://127.0.0.1:8000").rstrip("/")
_HOST = os.environ.get("W2A_OPENCODE_HOST", "127.0.0.1")
_PORT = int(os.environ.get("W2A_OPENCODE_PORT", "8010"))
_CACHE_TTL = float(os.environ.get("W2A_OPENCODE_CACHE_TTL", "300"))
_REQUEST_TIMEOUT = float(os.environ.get("W2A_OPENCODE_TIMEOUT", "930"))

_TOOL_SENTINEL = "__W2A_TOOL_CALL__"


@dataclass
class _CachedResult:
    expires_at: float
    status: int
    headers: dict[str, str]
    payload: dict[str, Any]


class OpenCodeBridge:
    def __init__(self, upstream: str = _UPSTREAM, cache_ttl: float = _CACHE_TTL) -> None:
        self.upstream = upstream.rstrip("/")
        self.cache_ttl = cache_ttl
        self._session: ClientSession | None = None
        self._inflight: dict[str, asyncio.Task[_CachedResult]] = {}
        self._cache: dict[str, _CachedResult] = {}
        self._lock = asyncio.Lock()

    async def start(self, app: web.Application) -> None:
        self._session = ClientSession(timeout=ClientTimeout(total=_REQUEST_TIMEOUT))

    async def close(self, app: web.Application) -> None:
        if self._session is not None:
            await self._session.close()

    @staticmethod
    def _fingerprint(body: dict[str, Any]) -> str:
        # stream is transport-only.  The logical model turn is otherwise the
        # same, and must coalesce across a reconnect that changes stream mode.
        logical = dict(body)
        logical.pop("stream", None)
        raw = json.dumps(logical, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def _tool_instructions(tools: list[dict[str, Any]]) -> str:
        compact = []
        for item in tools:
            if item.get("type") != "function":
                continue
            fn = item.get("function") or {}
            compact.append(
                {
                    "name": fn.get("name", ""),
                    "description": fn.get("description", ""),
                    "parameters": fn.get("parameters") or {"type": "object"},
                }
            )
        spec = json.dumps(compact, ensure_ascii=False, separators=(",", ":"))
        return (
            "\n\n[OpenCode tool protocol]\n"
            "You can request local tools, but you cannot execute them yourself. "
            "Available tools are JSON below.\n"
            f"{spec}\n"
            "When a tool is required, output ONLY one JSON object in exactly this shape:\n"
            f'{{"{_TOOL_SENTINEL}":true,"name":"tool_name","arguments":{{...}}}}\n'
            "Do not wrap it in markdown. Do not add prose before or after it. "
            "If no tool is required, answer normally."
        )

    @staticmethod
    def _normalise_messages(body: dict[str, Any]) -> list[dict[str, Any]]:
        messages = [dict(m) for m in body.get("messages") or []]
        # Web2API currently ignores role=tool. Convert tool results into user
        # context so the web model sees the result on the next turn.
        normalised: list[dict[str, Any]] = []
        for msg in messages:
            role = msg.get("role")
            if role == "tool":
                tool_call_id = msg.get("tool_call_id", "")
                normalised.append(
                    {
                        "role": "user",
                        "content": f"[Tool result {tool_call_id}]\n{msg.get('content', '')}",
                    }
                )
                continue
            if role == "assistant" and msg.get("tool_calls"):
                calls = []
                for call in msg.get("tool_calls") or []:
                    fn = call.get("function") or {}
                    calls.append(
                        f"{call.get('id', '')}: {fn.get('name', '')}({fn.get('arguments', '{}')})"
                    )
                msg["content"] = (msg.get("content") or "") + "\n[Assistant tool request]\n" + "\n".join(calls)
                msg.pop("tool_calls", None)
            normalised.append(msg)
        return normalised

    @classmethod
    def _prepare_upstream_body(cls, body: dict[str, Any]) -> dict[str, Any]:
        upstream = dict(body)
        upstream["stream"] = False
        upstream["messages"] = cls._normalise_messages(body)
        tools = body.get("tools") or []
        if tools:
            instructions = cls._tool_instructions(tools)
            system_index = next(
                (i for i, msg in enumerate(upstream["messages"]) if msg.get("role") == "system"),
                None,
            )
            if system_index is None:
                upstream["messages"].insert(0, {"role": "system", "content": instructions})
            else:
                current = upstream["messages"][system_index].get("content") or ""
                upstream["messages"][system_index]["content"] = current + instructions
        upstream.pop("tools", None)
        upstream.pop("tool_choice", None)
        upstream.pop("parallel_tool_calls", None)
        return upstream

    @staticmethod
    def _parse_tool_call(text: str) -> tuple[str, dict[str, Any]] | None:
        candidate = text.strip()
        if candidate.startswith("```"):
            lines = candidate.splitlines()
            if len(lines) >= 3 and lines[-1].strip().startswith("```"):
                candidate = "\n".join(lines[1:-1]).strip()
                if candidate.startswith("json\n"):
                    candidate = candidate[5:].strip()
        try:
            data = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(data, dict) or data.get(_TOOL_SENTINEL) is not True:
            return None
        name = data.get("name")
        arguments = data.get("arguments", {})
        if not isinstance(name, str) or not name:
            return None
        if not isinstance(arguments, dict):
            return None
        return name, arguments

    @classmethod
    def _translate_completion(cls, payload: dict[str, Any]) -> dict[str, Any]:
        choices = payload.get("choices") or []
        if not choices:
            return payload
        choice = choices[0]
        message = choice.get("message") or {}
        text = message.get("content")
        if not isinstance(text, str):
            return payload
        parsed = cls._parse_tool_call(text)
        if parsed is None:
            return payload
        name, arguments = parsed
        call_id = f"call_{uuid.uuid4().hex[:24]}"
        message["content"] = None
        message["tool_calls"] = [
            {
                "id": call_id,
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": json.dumps(arguments, ensure_ascii=False, separators=(",", ":")),
                },
            }
        ]
        choice["message"] = message
        choice["finish_reason"] = "tool_calls"
        return payload

    async def _do_upstream(self, body: dict[str, Any], auth_header: str | None) -> _CachedResult:
        assert self._session is not None
        headers = {"Content-Type": "application/json"}
        if auth_header:
            headers["Authorization"] = auth_header
        upstream_body = self._prepare_upstream_body(body)
        async with self._session.post(
            f"{self.upstream}/v1/chat/completions", headers=headers, json=upstream_body
        ) as resp:
            raw = await resp.read()
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                payload = {"error": {"message": raw.decode("utf-8", "replace"), "type": "server_error"}}
            if resp.status < 400:
                payload = self._translate_completion(payload)
            keep_headers = {}
            if "Retry-After" in resp.headers:
                keep_headers["Retry-After"] = resp.headers["Retry-After"]
            return _CachedResult(
                expires_at=time.monotonic() + self.cache_ttl,
                status=resp.status,
                headers=keep_headers,
                payload=payload,
            )

    async def _get_result(self, key: str, body: dict[str, Any], auth_header: str | None) -> _CachedResult:
        now = time.monotonic()
        async with self._lock:
            stale = [k for k, v in self._cache.items() if v.expires_at <= now]
            for k in stale:
                self._cache.pop(k, None)
            cached = self._cache.get(key)
            if cached is not None:
                return cached
            task = self._inflight.get(key)
            if task is None:
                task = asyncio.create_task(self._do_upstream(body, auth_header))
                self._inflight[key] = task

        try:
            # shield is deliberate: cancellation/disconnect of one HTTP client
            # must not cancel the logical ChatGPT turn shared by reconnects.
            result = await asyncio.shield(task)
        finally:
            if task.done():
                async with self._lock:
                    self._inflight.pop(key, None)
                    if not task.cancelled() and task.exception() is None:
                        self._cache[key] = task.result()
        return result

    @staticmethod
    def _as_sse(payload: dict[str, Any]) -> bytes:
        choices = payload.get("choices") or []
        if not choices:
            return f"data: {json.dumps(payload)}\n\ndata: [DONE]\n\n".encode()
        choice = choices[0]
        message = choice.get("message") or {}
        delta: dict[str, Any] = {"role": "assistant"}
        if message.get("tool_calls"):
            delta["tool_calls"] = message["tool_calls"]
        else:
            delta["content"] = message.get("content") or ""
        chunk = {
            "id": payload.get("id", f"chatcmpl-{uuid.uuid4().hex[:29]}"),
            "object": "chat.completion.chunk",
            "created": payload.get("created", int(time.time())),
            "model": payload.get("model", ""),
            "choices": [
                {
                    "index": 0,
                    "delta": delta,
                    "finish_reason": choice.get("finish_reason", "stop"),
                }
            ],
        }
        return f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n".encode()

    async def chat(self, request: web.Request) -> web.StreamResponse:
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response(
                {"error": {"message": "Invalid JSON", "type": "invalid_request_error"}}, status=400
            )
        key = self._fingerprint(body)
        result = await self._get_result(key, body, request.headers.get("Authorization"))
        if body.get("stream") and result.status < 400:
            return web.Response(
                body=self._as_sse(result.payload),
                status=result.status,
                headers={**result.headers, "Cache-Control": "no-cache"},
                content_type="text/event-stream",
            )
        return web.json_response(result.payload, status=result.status, headers=result.headers)

    async def proxy_get(self, request: web.Request) -> web.Response:
        assert self._session is not None
        path = request.match_info["tail"]
        headers = {}
        if auth := request.headers.get("Authorization"):
            headers["Authorization"] = auth
        async with self._session.get(f"{self.upstream}/{path}", headers=headers) as resp:
            raw = await resp.read()
            return web.Response(body=raw, status=resp.status, content_type=resp.content_type)


def create_app() -> web.Application:
    bridge = OpenCodeBridge()
    app = web.Application(client_max_size=10 * 1024 * 1024)
    app["bridge"] = bridge
    app.on_startup.append(bridge.start)
    app.on_cleanup.append(bridge.close)
    app.router.add_post("/v1/chat/completions", bridge.chat)
    app.router.add_post("/chat/completions", bridge.chat)
    app.router.add_get("/{tail:.*}", bridge.proxy_get)
    return app


def main() -> None:
    web.run_app(create_app(), host=_HOST, port=_PORT)


if __name__ == "__main__":
    main()
