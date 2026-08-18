"""Shared configuration helpers for the OpenCode integration."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_PROVIDER = "chatgpt-web"
DEFAULT_UPSTREAM = "http://127.0.0.1:8080"
DEFAULT_BRIDGE_URL = "http://127.0.0.1:8010/v1"
DEFAULT_TIMEOUT = 930
# The upstream detector defaults (90s/120s after visible text stops changing)
# are intentionally conservative for general browser automation. OpenCode tool
# turns are tiny JSON envelopes, so waiting that long after visible output has
# stopped makes the agent look frozen. The setup-managed instance uses a much
# shorter post-content idle budget while keeping the long first-content budget
# required by reasoning models.
OPENCODE_STREAM_IDLE_TIMEOUT = 20.0


def state_dir() -> Path:
    return Path.home() / ".chatgpt-web2api"


def core_config_path() -> Path:
    return state_dir() / "config.json"


def key_file_path() -> Path:
    return state_dir() / "opencode-api-key"


def global_opencode_config_path() -> Path:
    base = Path.home() / ".config" / "opencode"
    for name in ("opencode.json", "opencode.jsonc"):
        candidate = base / name
        if candidate.exists():
            return candidate
    return base / "opencode.json"


def strip_jsonc(text: str) -> str:
    """Remove comments and trailing commas while preserving quoted strings."""
    out: list[str] = []
    i = 0
    in_string = escaped = line_comment = block_comment = False
    while i < len(text):
        char = text[i]
        nxt = text[i + 1] if i + 1 < len(text) else ""
        if line_comment:
            if char in "\r\n":
                line_comment = False
                out.append(char)
            i += 1
            continue
        if block_comment:
            if char == "*" and nxt == "/":
                block_comment = False
                i += 2
            else:
                if char in "\r\n":
                    out.append(char)
                i += 1
            continue
        if in_string:
            out.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            i += 1
            continue
        if char == '"':
            in_string = True
            out.append(char)
            i += 1
        elif char == "/" and nxt == "/":
            line_comment = True
            i += 2
        elif char == "/" and nxt == "*":
            block_comment = True
            i += 2
        else:
            out.append(char)
            i += 1

    text = "".join(out)
    out = []
    i = 0
    in_string = escaped = False
    while i < len(text):
        char = text[i]
        if in_string:
            out.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            i += 1
            continue
        if char == '"':
            in_string = True
            out.append(char)
            i += 1
            continue
        if char == ",":
            j = i + 1
            while j < len(text) and text[j].isspace():
                j += 1
            if j < len(text) and text[j] in "}]":
                i += 1
                continue
        out.append(char)
        i += 1
    return "".join(out)


def load_object(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    value = json.loads(strip_jsonc(path.read_text(encoding="utf-8")))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object in {path}")
    return value


def backup(path: Path) -> Path | None:
    if not path.exists():
        return None
    target = path.with_name(f"{path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(path, target)
    return target


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def write_secret(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value.rstrip() + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def normalize_url(value: str, *, v1: bool) -> str:
    value = value.strip().rstrip("/")
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"Invalid HTTP URL: {value}")
    has_v1 = parsed.path.rstrip("/").endswith("/v1")
    if v1 and not has_v1:
        value += "/v1"
    elif not v1 and has_v1:
        value = value[: -len("/v1")]
    return value.rstrip("/")


def origin(value: str) -> str:
    parsed = urllib.parse.urlparse(value)
    netloc = parsed.netloc
    return urllib.parse.urlunparse((parsed.scheme, netloc, "", "", "", "")).rstrip("/")


def is_loopback(value: str) -> bool:
    return urllib.parse.urlparse(value).hostname in {"127.0.0.1", "localhost", "::1"}


def request_json(
    url: str, api_key: str | None, timeout: float = 5
) -> tuple[int, dict[str, Any]]:
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", "replace")
            value = json.loads(raw) if raw else {}
            return response.status, value if isinstance(value, dict) else {"data": value}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            value = {"error": {"message": raw}}
        return exc.code, value if isinstance(value, dict) else {"data": value}


def model_catalog(upstream: str, api_key: str | None) -> list[str]:
    try:
        status, payload = request_json(
            f"{normalize_url(upstream, v1=False)}/v1/models", api_key, 8
        )
    except (OSError, ValueError, json.JSONDecodeError):
        return []
    if status >= 400 or not isinstance(payload.get("data"), list):
        return []
    return [
        item["id"]
        for item in payload["data"]
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    ]


def choose_model(models: list[str]) -> str:
    for marker in ("sol", "auto", "thinking", "reasoning", "gpt-5"):
        for model in models:
            if marker in model.lower():
                return model
    return models[0] if models else "auto"


def reasoning_model(model: str) -> bool:
    value = model.lower()
    return any(
        marker in value
        for marker in ("sol", "thinking", "reasoning", "research", "o1", "o3", "o4", "5-5")
    )


def project_config_path(project_dir: Path) -> Path:
    candidates = [
        project_dir / "opencode.json",
        project_dir / "opencode.jsonc",
        project_dir / ".opencode" / "opencode.json",
        project_dir / ".opencode" / "opencode.jsonc",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def _shorten_idle_budget(config: dict[str, Any], key: str) -> None:
    current = config.get(key)
    try:
        current_value = float(current) if current is not None else OPENCODE_STREAM_IDLE_TIMEOUT
    except (TypeError, ValueError):
        current_value = OPENCODE_STREAM_IDLE_TIMEOUT
    config[key] = min(current_value, OPENCODE_STREAM_IDLE_TIMEOUT)


def configure_core(path: Path, api_key: str, upstream: str, model: str) -> Path | None:
    config = load_object(path)
    previous = backup(path)
    parsed = urllib.parse.urlparse(normalize_url(upstream, v1=False))
    config["host"] = "127.0.0.1"
    config["port"] = parsed.port or (443 if parsed.scheme == "https" else 8080)
    keys = config.get("api_keys")
    if not isinstance(keys, list):
        keys = []
    if api_key not in keys:
        keys.append(api_key)
    config["api_keys"] = keys
    config["request_timeout"] = max(
        int(config.get("request_timeout", 0) or 0), DEFAULT_TIMEOUT
    )
    config["detector_hard_timeout_seconds"] = max(
        float(config.get("detector_hard_timeout_seconds", 0) or 0), 900
    )
    config["detector_reasoning_first_content_timeout_seconds"] = max(
        float(config.get("detector_reasoning_first_content_timeout_seconds", 0) or 0), 300
    )
    config["detector_default_first_content_timeout_seconds"] = max(
        float(config.get("detector_default_first_content_timeout_seconds", 0) or 0), 90
    )
    _shorten_idle_budget(config, "detector_reasoning_stream_idle_timeout_seconds")
    _shorten_idle_budget(config, "detector_default_stream_idle_timeout_seconds")
    if model != "auto" and not config.get("default_model"):
        config["default_model"] = model
    write_json(path, config)
    return previous


def configure_opencode(
    path: Path,
    *,
    provider_id: str,
    bridge_url: str,
    key_file: Path,
    model: str,
    model_name: str,
    set_default: bool,
    safe_permissions: bool,
) -> Path | None:
    config = load_object(path)
    previous = backup(path)
    providers = config.get("provider")
    if not isinstance(providers, dict):
        providers = {}
        config["provider"] = providers
    provider = providers.get(provider_id)
    if not isinstance(provider, dict):
        provider = {}
    options = provider.get("options") if isinstance(provider.get("options"), dict) else {}
    models = provider.get("models") if isinstance(provider.get("models"), dict) else {}
    options.update(
        {
            "baseURL": normalize_url(bridge_url, v1=True),
            "apiKey": f"{{file:{key_file.expanduser().resolve().as_posix()}}}",
            "timeout": False,
            "headerTimeout": 960000,
            "chunkTimeout": 120000,
        }
    )
    models[model] = {
        "name": model_name,
        "tool_call": True,
        "reasoning": reasoning_model(model),
    }
    provider.update(
        {
            "npm": "@ai-sdk/openai-compatible",
            "name": "ChatGPT Web2API",
            "options": options,
            "models": models,
        }
    )
    providers[provider_id] = provider
    config.setdefault("$schema", "https://opencode.ai/config.json")
    if set_default or not config.get("model"):
        config["model"] = f"{provider_id}/{model}"
    if safe_permissions and "permission" not in config:
        config["permission"] = {
            "edit": "ask",
            "bash": "ask",
            "external_directory": "ask",
        }
    write_json(path, config)
    return previous


def self_command() -> list[str]:
    if getattr(sys, "frozen", False):
        return [str(Path(sys.executable).resolve())]
    return [str(Path(sys.executable).resolve()), "-m", "chatgpt_web2api.opencode_setup"]


def core_command(config: Path) -> list[str]:
    return [
        str(Path(sys.executable).resolve()),
        "-m",
        "chatgpt_web2api",
        "start",
        "--config",
        str(config),
    ]


def write_launchers(
    directory: Path,
    *,
    config: Path,
    upstream: str,
    bridge_url: str,
    start_core: bool,
    start_bridge: bool,
) -> tuple[Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    command = self_command() + [
        "start",
        "--config",
        str(config.resolve()),
        "--upstream",
        normalize_url(upstream, v1=False),
        "--bridge-url",
        normalize_url(bridge_url, v1=True),
    ]
    if not start_core:
        command.append("--no-core")
    if not start_bridge:
        command.append("--no-bridge")

    cmd = directory / "start-opencode-web2api.cmd"
    cmd.write_text(
        "@echo off\r\ntitle ChatGPT Web2API for OpenCode\r\n"
        + subprocess.list2cmdline(command)
        + "\r\nif errorlevel 1 pause\r\n",
        encoding="utf-8",
    )
    sh = directory / "start-opencode-web2api.sh"
    sh.write_text(
        "#!/bin/sh\nexec " + " ".join(shlex.quote(part) for part in command) + "\n",
        encoding="utf-8",
    )
    try:
        sh.chmod(0o700)
    except OSError:
        pass
    return cmd, sh


def read_key(path: Path) -> str | None:
    if not path.exists():
        return None
    value = path.read_text(encoding="utf-8").strip()
    return value or None