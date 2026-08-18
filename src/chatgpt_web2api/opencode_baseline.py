"""Keep the stock Web2API path untouched and expose it as an OpenCode control provider."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .opencode_setup_common import load_object, normalize_url, reasoning_model, write_json

DIRECT_PROVIDER = "chatgpt-web-direct"

# Exact values written by older OpenCode installers.  Only remove a value when
# it still equals the value our installer injected; custom user values are left
# alone.  Once removed, Web2API falls back to its own built-in defaults.
_INJECTED_CORE_VALUES: dict[str, Any] = {
    "request_timeout": 930,
    "detector_hard_timeout_seconds": 900.0,
    "detector_reasoning_first_content_timeout_seconds": 300.0,
    "detector_default_first_content_timeout_seconds": 90.0,
    "detector_reasoning_stream_idle_timeout_seconds": 20.0,
    "detector_default_stream_idle_timeout_seconds": 20.0,
}


def restore_stock_core_tuning(path: Path) -> bool:
    """Remove only detector/request tuning previously injected by our installer."""
    config = load_object(path)
    changed = False
    for key, injected in _INJECTED_CORE_VALUES.items():
        current = config.get(key)
        try:
            matches = float(current) == float(injected)
        except (TypeError, ValueError):
            matches = current == injected
        if key in config and matches:
            config.pop(key, None)
            changed = True
    if changed:
        write_json(path, config)
    return changed


def configure_direct_provider(
    path: Path,
    *,
    upstream: str,
    key_file: Path,
    model: str,
    model_name: str,
    set_default: bool,
) -> None:
    """Add an A/B control provider that talks directly to stock Web2API.

    This provider deliberately bypasses the tool sidecar.  It is the baseline
    used to prove that Web2API/Chrome request-return works before tool protocol
    translation is involved.
    """
    config = load_object(path)
    providers = config.get("provider")
    if not isinstance(providers, dict):
        providers = {}
        config["provider"] = providers

    providers[DIRECT_PROVIDER] = {
        "npm": "@ai-sdk/openai-compatible",
        "name": "ChatGPT Web2API — direct baseline",
        "options": {
            "baseURL": normalize_url(upstream, v1=True),
            "apiKey": f"{{file:{key_file.expanduser().resolve().as_posix()}}}",
        },
        "models": {
            model: {
                "name": f"{model_name} — direct",
                "tool_call": False,
                "reasoning": reasoning_model(model),
            }
        },
    }
    if set_default:
        config["model"] = f"{DIRECT_PROVIDER}/{model}"
    write_json(path, config)
