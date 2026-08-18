"""OpenCode compatibility bridge for ChatGPT-Web2API.

This sidecar keeps the browser-facing Web2API core unchanged while adding the
OpenAI Chat function-calling contract expected by OpenCode:

* OpenAI ``tools`` are encoded into a strict text protocol for ChatGPT web;
* a model tool request is validated and translated back to ``tool_calls``;
* OpenCode ``role=tool`` results are made visible to the web model;
* identical in-flight turns are coalesced and shielded from client disconnects.
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


class ToolProtocolError(ValueError):
    """A model/client tool-protocol violation that must not execute a tool."""

    def __init__(self, message: str, *, code: str = "invalid_tool_call", status: int = 502) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


@dataclass
class _CachedResult:
    expires_at: float
    status: int
    headers: dict[str, str]
    payload: dict[str, Any]
    cacheable: bool = True


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
    def _fingerprint(body: dict[str, Any], auth_header: str | None = None) -> str:
        """Hash the logical turn, excluding streaming-only transport fields."""
        logical = dict(body)
        logical.pop("stream", None)
        logical.pop("stream_options", None)
        envelope = {
            "body": logical,
            # Isolate replay state across credentials without storing the key.
            "auth": hashlib.sha256((auth_header or "").encode("utf-8")).hexdigest(),
        }
        raw = json.dumps(envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def _function_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for item in tools:
            if item.get("type") != "function":
                continue
            fn = item.get("function") or {}
            name = fn.get("name")
            if isinstance(name, str) and name:
                result.append(item)
        return result

    @staticmethod
    def _tool_choice(body: dict[str, Any]) -> tuple[str, str | None]:
        choice = body.get("tool_choice", "auto")
        if choice is None:
            return "auto", None
        if isinstance(choice, str):
            if choice not in {"auto", "none", "required"}:
                raise ToolProtocolError(
                    f"Unsupported tool_choice value: {choice}",
                    code="invalid_tool_choice",
                    status=400,
                )
            return choice, None
        if isinstance(choice, dict):
            fn = choice.get("function") or {}
            name = fn.get("name")
            if choice.get("type") != "function" or not isinstance(name, str) or not name:
                raise ToolProtocolError(
                    "Invalid named tool_choice",
                    code="invalid_tool_choice",
                    status=400,
                )
            return "named", name
        raise ToolProtocolError(
            "Invalid tool_choice type",
            code="invalid_tool_choice",
            status=400,
        )

    @classmethod
    def _tool_instructions(cls, tools: list[dict[str, Any]], body: dict[str, Any]) -> str:
        compact = []
        for item in cls._function_tools(tools):
            fn = item.get("function") or {}
            compact.append(
                {
                    "name": fn.get("name", ""),
                    "description": fn.get("description", ""),
                    "parameters": fn.get("parameters") or {"type": "object"},
                }
            )

        mode, named = cls._tool_choice(body)
        names = {item["name"] for item in compact}
        if mode == "required" and not compact:
            raise ToolProtocolError(
                "tool_choice=required but no function tools were supplied",
                code="invalid_tool_choice",
                status=400,
            )
        if mode == "named" and named not in names:
            raise ToolProtocolError(
                f"Named tool_choice refers to unknown tool: {named}",
                code="invalid_tool_choice",
                status=400,
            )

        spec = json.dumps(compact, ensure_ascii=False, separators=(",", ":"))
        policy = "Use a tool only when it is needed."
        if mode == "required":
            policy = "You MUST request exactly one of the available tools in this response."
        elif mode == "named":
            policy = f"You MUST request the tool named {json.dumps(named)} in this response."

        return (
            "\n\n[OpenCode tool protocol]\n"
            "You are connected to OpenCode local tools. You cannot execute these tools yourself.\n"
            "Available function tools are listed as JSON below:\n"
            f"{spec}\n"
            f"{policy}\n"
            "When requesting a tool, output ONLY one JSON object in exactly this shape:\n"
            f'{{"{_TOOL_SENTINEL}":true,"name":"tool_name","arguments":{{...}}}}\n'
            "The name MUST exactly match an available tool. arguments MUST be a JSON object.\n"
            "Do not add prose before or after the object.\n"
            "After OpenCode executes the tool, its result will be supplied in the next request.\n"
            "If tool use is optional and no tool is needed, answer normally."
        )

    @staticmethod
    def _normalise_messages(body: dict[str, Any]) -> list[dict[str, Any]]:
        messages = [dict(m) for m in body.get("messages") or []]
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
        tools = cls._function_tools(body.get("tools") or [])
        mode, _ = cls._tool_choice(body)

        if tools and mode != "none":
            instructions = cls._tool_instructions(tools, body)
            system_index = next(
                (i for i, msg in enumerate(upstream["messages"]) if msg.get("role") == "system"),
                None,
            )
            if system_index is None:
                upstream["messages"].insert(0, {"role": "system", "content": instructions})
            else:
                current = upstream["messages"][system_index].get("content") or ""
                upstream["messages"][system_index]["content"] = current + instructions
        elif mode in {"required", "named"}:
            raise ToolProtocolError(
                "A required tool_choice was supplied without a usable function tool",
                code="invalid_tool_choice",
                status=400,
            )

        upstream.pop("tools", None)
        upstream.pop("tool_choice", None)
        upstream.pop("parallel_tool_calls", None)
        upstream.pop("stream_options", None)
        return upstream

    @staticmethod
    def _extract_sentinel_object(text: str) -> str | None:
        """Extract a JSON object containing our private sentinel from light prose/fences."""
        marker = f'"{_TOOL_SENTINEL}"'
        marker_at = text.find(marker)
        if marker_at < 0:
            return None
        start = text.rfind("{", 0, marker_at + 1)
        if start < 0:
            return None

        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return text[start : index + 1]
        return None

    @classmethod
    def _parse_tool_call(cls, text: str) -> tuple[str, dict[str, Any]] | None:
        candidate = text.strip()
        candidates = [candidate]
        extracted = cls._extract_sentinel_object(candidate)
        if extracted and extracted != candidate:
            candidates.append(extracted)

        data: Any = None
        for item in candidates:
            if item.startswith("```"):
                lines = item.splitlines()
                if len(lines) >= 3 and lines[-1].strip().startswith("```"):
                    first = lines[0].strip().lower()
                    if first in {"```", "```json"}:
                        item = "\n".join(lines[1:-1]).strip()
            try:
                parsed = json.loads(item)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(parsed, dict) and parsed.get(_TOOL_SENTINEL) is True:
                data = parsed
                break
        if not isinstance(data, dict):
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

    @classmethod
    def _validate_tool_call(cls, body: dict[str, Any], name: str) -> None:
        tools = cls._function_tools(body.get("tools") or [])
        names = {(item.get("function") or {}).get("name") for item in tools}
        mode, named = cls._tool_choice(body)
        if mode == "none":
            raise ToolProtocolError(
                "Model requested a tool while tool_choice=none",
                code="unexpected_tool_call",
            )
        if name not in names:
            raise ToolProtocolError(
                f"Model requested unavailable tool: {name}",
                code="unknown_tool_call",
            )
        if mode == "named" and name != named:
            raise ToolProtocolError(
                f"Model requested {name}, but tool_choice requires {named}",
                code="wrong_tool_call",
            )

    @classmethod
    def _translate_completion(
        cls, payload: dict[str, Any], body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
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
            if body is not None:
                mode, _ = cls._tool_choice(body)
                if mode in {"required", "named"} and cls._function_tools(body.get("tools") or []):
                    raise ToolProtocolError(
                        "Model returned text although a tool call was required",
                        code="tool_call_required",
                    )
            return payload

        name, arguments = parsed
        if body is not None:
            cls._validate_tool_call(body, name)
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

    @staticmethod
    def _protocol_error_result(exc: ToolProtocolError, ttl: float) -> _CachedResult:
        return _CachedResult(
            expires_at=time.monotonic() + ttl,
            status=exc.status,
            headers={},
            payload={
                "error": {
                    "message": str(exc),
                    "type": "invalid_request_error" if exc.status < 500 else "server_error",
                    "code": exc.code,
                }
            },
            cacheable=True,
        )

    async def _do_upstream(self, body: dict[str, Any], auth_header: str | None) -> _CachedResult:
        assert self._session is not None
        try:
            upstream_body = self._prepare_upstream_body(body)
        except ToolProtocolError as exc:
            return self._protocol_error_result(exc, self.cache_ttl)

        headers = {"Content-Type": "application/json"}
        if auth_header:
            headers["Authorization"] = auth_header
        async with self._session.post(
            f"{self.upstream}/v1/chat/completions", headers=headers, json=upstream_body
        ) as resp:
            raw = await resp.read()
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                payload = {
                    "error": {
                        "message": raw.decode("utf-8", "replace"),
                        "type": "server_error",
                    }
                }
            status = resp.status
            if status < 400:
                try:
                    payload = self._translate_completion(payload, body)
                except ToolProtocolError as exc:
                    return self._protocol_error_result(exc, self.cache_ttl)
            keep_headers = {}
            if "Retry-After" in resp.headers:
                keep_headers["Retry-After"] = resp.headers["Retry-After"]
            return _CachedResult(
                expires_at=time.monotonic() + self.cache_ttl,
                status=status,
                headers=keep_headers,
                payload=payload,
                # A 429/401/5xx must remain retryable rather than poisoning the
                # replay cache for the full TTL.
                cacheable=status < 400,
            )

    async def _finalize_task(self, key: str, task: asyncio.Task[_CachedResult]) -> None:
        """Move a completed logical turn from inflight to replay cache."""
        async with self._lock:
            if self._inflight.get(key) is not task:
                return
            self._inflight.pop(key, None)
            if task.cancelled():
                return
            try:
                exc = task.exception()
            except asyncio.CancelledError:
                return
            if exc is None:
                result = task.result()
                if result.cacheable:
                    self._cache[key] = result

    def _schedule_finalize(self, key: str, task: asyncio.Task[_CachedResult]) -> None:
        asyncio.create_task(self._finalize_task(key, task))

    async def _get_result(
        self,
        key: str,
        body: dict[str, Any],
        auth_header: str | None,
    ) -> _CachedResult:
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
                task.add_done_callback(lambda done, k=key: self._schedule_finalize(k, done))

        # Deliberate: cancelling/disconnecting one HTTP client must not cancel
        # the shared logical ChatGPT turn. A retry joins the same task.
        return await asyncio.shield(task)

    @staticmethod
    def _as_sse(payload: dict[str, Any]) -> bytes:
        choices = payload.get("choices") or []
        if not choices:
            return f"data: {json.dumps(payload)}\n\ndata: [DONE]\n\n".encode()

        choice = choices[0]
        message = choice.get("message") or {}
        cid = payload.get("id", f"chatcmpl-{uuid.uuid4().hex[:29]}")
        created = payload.get("created", int(time.time()))
        model = payload.get("model", "")
        events: list[dict[str, Any]] = []

        tool_calls = message.get("tool_calls") or []
        if tool_calls:
            deltas = []
            for index, call in enumerate(tool_calls):
                fn = call.get("function") or {}
                deltas.append(
                    {
                        "index": index,
                        "id": call.get("id"),
                        "type": "function",
                        "function": {
                            "name": fn.get("name"),
                            "arguments": fn.get("arguments", ""),
                        },
                    }
                )
            events.append(
                {
                    "id": cid,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"tool_calls": deltas},
                            "finish_reason": None,
                        }
                    ],
                }
            )
        else:
            content = message.get("content")
            if isinstance(content, str) and content:
                events.append(
                    {
                        "id": cid,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"content": content},
                                "finish_reason": None,
                            }
                        ],
                    }
                )

        events.append(
            {
                "id": cid,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "delta": {},
                        "finish_reason": choice.get("finish_reason", "stop"),
                    }
                ],
            }
        )

        usage = payload.get("usage")
        if isinstance(usage, dict):
            events.append(
                {
                    "id": cid,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "choices": [],
                    "usage": usage,
                }
            )

        data = "".join(f"data: {json.dumps(event)}\n\n" for event in events)
        return f"{data}data: [DONE]\n\n".encode()

    async def chat(self, request: web.Request) -> web.StreamResponse:
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response(
                {"error": {"message": "Invalid JSON", "type": "invalid_request_error"}},
                status=400,
            )
        auth_header = request.headers.get("Authorization")
        key = self._fingerprint(body, auth_header)
        result = await self._get_result(key, body, auth_header)
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
