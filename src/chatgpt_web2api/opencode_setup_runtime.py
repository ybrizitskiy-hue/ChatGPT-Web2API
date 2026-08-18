"""Runtime supervision and diagnostics for the OpenCode integration."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import time
import urllib.parse
from typing import Any

from .opencode_setup_common import (
    core_command,
    global_opencode_config_path,
    is_loopback,
    load_object,
    model_catalog,
    normalize_url,
    project_config_path,
    read_key,
    request_json,
    self_command,
)


def healthy(url: str, api_key: str | None) -> bool:
    """Return True only for a usable endpoint.

    Core Web2API deliberately returns HTTP 200 for ``degraded`` and ``broken``
    health payloads so operators can inspect diagnostics. Treat those states as
    not ready here; otherwise the launcher could report success while CDP is
    disconnected. ``starting`` is accepted because core only emits it after
    Chrome and the driver are connected but before the first request.
    """
    try:
        status, payload = request_json(url, api_key, 2)
        if not 200 <= status < 400:
            return False
        return payload.get("status") not in {"broken", "degraded"}
    except (OSError, ValueError, json.JSONDecodeError):
        return False


def wait_healthy(url: str, api_key: str | None, deadline: float) -> bool:
    while time.monotonic() < deadline:
        if healthy(url, api_key):
            return True
        time.sleep(1)
    return False


def spawn(command: list[str]) -> subprocess.Popen[Any]:
    kwargs: dict[str, Any] = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(command, **kwargs)


def terminate(process: subprocess.Popen[Any]) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "nt":
            process.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        process.kill()


def bridge_base_url(bridge_url: str) -> str:
    value = normalize_url(bridge_url, v1=True)
    return value[: -len("/v1")]


def start(args: argparse.Namespace) -> int:
    upstream = normalize_url(args.upstream, v1=False)
    bridge_url = normalize_url(args.bridge_url, v1=True)
    bridge_base = bridge_base_url(bridge_url)
    api_key = read_key(args.key_file.expanduser())
    children: list[subprocess.Popen[Any]] = []
    try:
        if not healthy(f"{upstream}/health", api_key):
            if args.no_core or not is_loopback(upstream):
                print(f"Web2API is not reachable at {upstream}")
                return 1
            print("Starting ChatGPT-Web2API...")
            children.append(spawn(core_command(args.config.expanduser())))
            if not wait_healthy(
                f"{upstream}/health", api_key, time.monotonic() + args.startup_timeout
            ):
                print("Web2API did not become reachable. Check the Chrome window and logs.")
                return 1
        else:
            print(f"Web2API already reachable at {upstream}")

        try:
            status, _ = request_json(f"{upstream}/v1/models", api_key, 5)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"Web2API model endpoint is unreachable: {exc}")
            return 1
        if status == 401:
            print(
                "Web2API rejected the configured API key. Restart a local server after setup, "
                "or use the remote key."
            )
            return 1
        if status >= 400:
            print(f"Web2API model endpoint returned HTTP {status}.")
            return 1

        if not healthy(f"{bridge_base}/bridge/health", api_key):
            if args.no_bridge or not is_loopback(bridge_base):
                print(f"OpenCode bridge is not reachable at {bridge_base}")
                return 1
            parsed = urllib.parse.urlparse(bridge_url)
            if parsed.path.rstrip("/") != "/v1":
                print("A locally managed bridge URL cannot contain a path prefix before /v1.")
                return 2
            command = self_command() + [
                "serve",
                "--upstream",
                upstream,
                "--host",
                parsed.hostname or "127.0.0.1",
                "--port",
                str(parsed.port or 8010),
                "--api-key-file",
                str(args.key_file.expanduser().resolve()),
            ]
            print("Starting OpenCode bridge...")
            children.append(spawn(command))
            if not wait_healthy(
                f"{bridge_base}/bridge/health", api_key, time.monotonic() + 30
            ):
                print("OpenCode bridge did not become reachable.")
                return 1
        else:
            print(f"OpenCode bridge already reachable at {bridge_base}")

        if args.launch_opencode:
            executable = shutil.which("opencode")
            if executable:
                children.append(spawn([executable]))
            else:
                print("OpenCode CLI was not found on PATH; open the desktop app manually.")
        print("Stack is ready.")
        print(f"  OpenCode baseURL: {bridge_url}")
        if is_loopback(upstream):
            print("  Sign in to ChatGPT in the dedicated Chrome window if prompted.")
        if not children:
            return 0
        print("Press Ctrl+C to stop processes started by this launcher.")
        while True:
            for process in children:
                code = process.poll()
                if code is not None:
                    print(f"A managed process exited with code {code}.")
                    return code or 1
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping...")
        return 0
    finally:
        for process in reversed(children):
            terminate(process)


def doctor(args: argparse.Namespace) -> int:
    upstream = normalize_url(args.upstream, v1=False)
    bridge_url = normalize_url(args.bridge_url, v1=True)
    bridge_base = bridge_base_url(bridge_url)
    key_file = args.key_file.expanduser()
    api_key = read_key(key_file)
    opencode_path = args.opencode_config or (
        global_opencode_config_path()
        if args.scope == "global"
        else project_config_path(args.project_dir.expanduser().resolve())
    )
    checks: list[tuple[str, bool, str]] = []
    checks.append(
        (
            "Web2API config",
            args.core_config.expanduser().exists() or not is_loopback(upstream),
            str(args.core_config),
        )
    )
    checks.append(("API key file", bool(api_key), str(key_file)))
    try:
        config = load_object(opencode_path)
        providers = config.get("provider")
        provider = providers.get(args.provider_id) if isinstance(providers, dict) else None
        options = provider.get("options") if isinstance(provider, dict) else None
        provider_ok = (
            isinstance(provider, dict)
            and isinstance(options, dict)
            and normalize_url(str(options.get("baseURL", "")), v1=True) == bridge_url
        )
        detail = str(opencode_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        provider_ok, detail = False, f"{opencode_path}: {exc}"
    checks.append(("OpenCode provider", provider_ok, detail))
    checks.append(
        ("Web2API health", healthy(f"{upstream}/health", api_key), f"{upstream}/health")
    )
    try:
        status, _ = request_json(f"{upstream}/v1/models", api_key, 5)
    except (OSError, ValueError, json.JSONDecodeError):
        status = 0
    checks.append(
        (
            "Web2API authentication",
            200 <= status < 400,
            f"HTTP {status}" if status else "unreachable",
        )
    )
    bridge_ok = healthy(f"{bridge_base}/bridge/health", api_key)
    checks.append(("Bridge health", bridge_ok, f"{bridge_base}/bridge/health"))
    models = model_catalog(bridge_base, api_key) if bridge_ok else []
    checks.append(("Model catalog", bool(models), ", ".join(models) if models else bridge_url))

    print("OpenCode integration doctor")
    for name, ok, detail in checks:
        print(f"  {'OK' if ok else 'FAIL':4}  {name}: {detail}")
    return 0 if all(ok for _name, ok, _detail in checks) else 1


def serve(args: argparse.Namespace) -> int:
    from aiohttp import web

    from .opencode_bridge_runtime import create_app

    api_key = read_key(args.api_key_file.expanduser())
    if not api_key:
        print(f"OpenCode bridge API key is missing or empty: {args.api_key_file}")
        return 2

    web.run_app(
        create_app(
            upstream=normalize_url(args.upstream, v1=False),
            cache_ttl=args.cache_ttl,
            request_timeout=args.timeout,
            cache_max_entries=args.cache_max_entries,
            heartbeat_interval=args.heartbeat_interval,
            api_key=api_key,
        ),
        host=args.host,
        port=args.port,
    )
    return 0
