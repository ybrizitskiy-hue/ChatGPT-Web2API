"""GitHub tool routing policy for the OpenCode sidecar.

Keep public read-only GitHub work on OpenCode-native web tools when available,
instead of making the model depend on a locally installed/authenticated ``gh``.
For deterministic public reads, the bridge can synthesize the first OpenAI
``webfetch`` tool call directly so ChatGPT never has to emulate tool selection.
This layer does not touch the Web2API browser/CDP core or session affinity.
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.parse
import uuid
from typing import Any

from aiohttp import web

from .opencode_bridge import _CachedResult
from .opencode_bridge_exact_session import (
    ExactSessionOpenCodeBridge,
    _SESSION_CONTEXT,
    _session_id_from_headers,
)
from .opencode_bridge_runtime import (
    BRIDGE_APP_KEY,
    DEFAULT_CACHE_MAX_ENTRIES,
    DEFAULT_CACHE_TTL,
    DEFAULT_HEARTBEAT_INTERVAL,
    DEFAULT_REQUEST_TIMEOUT,
    DEFAULT_UPSTREAM,
)

_LOG = logging.getLogger(__name__)

_PUBLIC_GITHUB_URL_RE = re.compile(
    r"https?://(?:www\.)?(?:github\.com|api\.github\.com)/[^\s<>()]+",
    re.IGNORECASE,
)
_GITHUB_WRITE_RE = re.compile(
    r"\b(push|merge|create|update|edit|delete|remove|close|reopen|comment|approve|"
    r"tag|release|fork)\b|"
    r"\bcommit\s+(?:this|these|the|my|our|changes?|files?)\b|"
    r"(запуш|пуш|смерж|см[её]рж|создай|обнови|измени|удали|закрой|переоткрой|"
    r"прокоммент|одобри|закоммить|тег|релиз|форк)",
    re.IGNORECASE,
)
_LATEST_COMMIT_READ_RE = re.compile(
    r"(?:\b(?:latest|last|newest|recent)\b.{0,48}\bcommit\b)"
    r"|(?:\bcommit\b.{0,48}\b(?:message|sha|hash)\b)"
    r"|(?:последн\w*.{0,48}коммит)"
    r"|(?:коммит.{0,48}(?:сообщен|sha|хеш|hash))",
    re.IGNORECASE,
)


class GitHubRoutingOpenCodeBridge(ExactSessionOpenCodeBridge):
    """Route public GitHub reads through deterministic OpenCode web tools."""

    @classmethod
    def _latest_user_message_text(cls, body: dict[str, Any]) -> str:
        for message in reversed(body.get("messages") or []):
            if isinstance(message, dict) and message.get("role") == "user":
                return cls._content_text(message.get("content"))
        return ""

    @classmethod
    def _public_github_url(cls, body: dict[str, Any]) -> str | None:
        match = _PUBLIC_GITHUB_URL_RE.search(cls._latest_user_message_text(body))
        if not match:
            return None
        return match.group(0).rstrip(".,;:!?)]}'\"")

    @classmethod
    def _public_github_read_intent(cls, body: dict[str, Any]) -> bool:
        text = cls._latest_user_message_text(body)
        return bool(_PUBLIC_GITHUB_URL_RE.search(text)) and not bool(_GITHUB_WRITE_RE.search(text))

    @classmethod
    def _available_tool_names(cls, body: dict[str, Any]) -> set[str]:
        return {
            str((tool.get("function") or {}).get("name"))
            for tool in cls._function_tools(body.get("tools") or [])
            if (tool.get("function") or {}).get("name")
        }

    @classmethod
    def _named_tool_choice(cls, name: str) -> dict[str, Any]:
        return {"type": "function", "function": {"name": name}}

    @classmethod
    def _force_opencode_tool_for_github(cls, body: dict[str, Any]) -> dict[str, Any]:
        forced = super()._force_opencode_tool_for_github(body)
        if forced is body:
            return body

        # Respect any explicit user/client choice. The parent only changes auto,
        # but keep the guard here so this policy remains safe if the parent evolves.
        original_mode, _ = cls._tool_choice(body)
        if original_mode != "auto" or not cls._public_github_read_intent(body):
            return forced

        names = cls._available_tool_names(body)
        preferred = "webfetch" if "webfetch" in names else "websearch" if "websearch" in names else None
        if not preferred:
            return forced

        routed = dict(forced)
        routed["tool_choice"] = cls._named_tool_choice(preferred)
        return routed

    @classmethod
    def _webfetch_arguments(cls, body: dict[str, Any]) -> dict[str, Any] | None:
        """Build safe arguments for the OpenCode webfetch schema."""
        source_url = cls._public_github_url(body)
        if not source_url:
            return None

        text = cls._latest_user_message_text(body)
        target_url = source_url
        response_format = "text" if "api.github.com" in source_url.lower() else "markdown"

        parsed = urllib.parse.urlparse(source_url)
        path_parts = [part for part in parsed.path.split("/") if part]
        if (
            parsed.netloc.lower() in {"github.com", "www.github.com"}
            and len(path_parts) == 2
            and _LATEST_COMMIT_READ_RE.search(text)
        ):
            owner = urllib.parse.quote(path_parts[0], safe="")
            repo = urllib.parse.quote(path_parts[1].removesuffix(".git"), safe="")
            target_url = f"https://api.github.com/repos/{owner}/{repo}/commits?per_page=1"
            response_format = "text"

        webfetch = next(
            (
                tool.get("function") or {}
                for tool in cls._function_tools(body.get("tools") or [])
                if (tool.get("function") or {}).get("name") == "webfetch"
            ),
            None,
        )
        if not isinstance(webfetch, dict):
            return None

        parameters = webfetch.get("parameters") or {}
        properties = parameters.get("properties") if isinstance(parameters, dict) else {}
        required = parameters.get("required") if isinstance(parameters, dict) else []
        properties = properties if isinstance(properties, dict) else {}
        required = required if isinstance(required, list) else []

        arguments: dict[str, Any] = {"url": target_url}
        if "format" in properties or "format" in required:
            arguments["format"] = response_format
        return arguments

    @classmethod
    def _synthetic_public_github_tool_call(
        cls, body: dict[str, Any]
    ) -> tuple[str, dict[str, Any]] | None:
        """Return a deterministic first tool call only for a new public GitHub read turn."""
        if cls._is_title_request(body):
            return None
        last = cls._last_non_system_message(body)
        if not last or last.get("role") != "user":
            return None
        if not cls._public_github_read_intent(body):
            return None

        mode, named = cls._tool_choice(body)
        if mode == "none" or (mode == "named" and named != "webfetch"):
            return None
        if "webfetch" not in cls._available_tool_names(body):
            return None

        arguments = cls._webfetch_arguments(body)
        if arguments is None:
            return None
        return "webfetch", arguments

    @classmethod
    def _synthetic_tool_payload(
        cls,
        body: dict[str, Any],
        name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        call_id = f"call_{uuid.uuid4().hex[:24]}"
        return {
            "id": f"chatcmpl-local-tool-{uuid.uuid4().hex[:16]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": body.get("model", "auto"),
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": call_id,
                                "type": "function",
                                "function": {
                                    "name": name,
                                    "arguments": json.dumps(
                                        arguments,
                                        ensure_ascii=False,
                                        separators=(",", ":"),
                                    ),
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }

    async def _do_upstream(
        self,
        body: dict[str, Any],
        auth_header: str | None,
    ) -> _CachedResult:
        synthetic = self._synthetic_public_github_tool_call(body)
        if synthetic is not None:
            name, arguments = synthetic
            _LOG.info("Synthetic OpenCode tool call: %s %s", name, arguments.get("url", ""))
            return _CachedResult(
                expires_at=time.monotonic() + self.cache_ttl,
                status=200,
                headers={},
                payload=self._synthetic_tool_payload(body, name, arguments),
                cacheable=True,
            )
        return await super()._do_upstream(body, auth_header)

    @classmethod
    def _tool_instructions(cls, tools: list[dict[str, Any]], body: dict[str, Any]) -> str:
        return (
            super()._tool_instructions(tools, body)
            + "\n[GitHub read routing]\n"
            "For a public read-only GitHub URL, prefer the OpenCode webfetch tool when it is "
            "available. It can fetch github.com or api.github.com directly without local GitHub "
            "CLI authentication. Do NOT choose bash+gh for a public read-only GitHub request when "
            "webfetch is available. If the selected web tool fails, report that tool result back "
            "through OpenCode and choose another OpenCode tool on the next turn; never fall back "
            "to a ChatGPT-native connector or authorization flow."
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
    bridge = GitHubRoutingOpenCodeBridge(
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
