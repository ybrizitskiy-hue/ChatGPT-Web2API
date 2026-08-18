"""Start, stop, and inspect the local Web2API + OpenCode bridge stack."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .opencode_setup import default_state_path, state_dir


def load_state(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"Stack state not found at {path}. Run chatgpt_web2api.opencode_setup first."
        ) from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid stack state at {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"Invalid stack state at {path}: expected an object")
    for key in ("upstream_url", "bridge_url"):
        if not isinstance(value.get(key), str) or not value[key]:
            raise RuntimeError(f"Invalid stack state at {path}: missing {key}")
    return value


def bridge_root(bridge_url: str) -> str:
    parsed = urlparse(bridge_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RuntimeError(f"Invalid bridge URL in state: {bridge_url}")
    return f"{parsed.scheme}://{parsed.netloc}"


def is_local_url(url: str) -> bool:
    hostname = (urlparse(url).hostname or "").lower()
    return hostname in {"127.0.0.1", "localhost", "::1"}


def probe(url: str, *, timeout: float = 3.0) -> tuple[bool, str]:
    request = Request(url, headers={"Accept": "application/json"})
    try:
        with urlopen(request, timeout=timeout) as response:
            body = response.read(400).decode("utf-8", "replace")
            return 200 <= response.status < 500, f"HTTP {response.status} {body[:160]}"
    except HTTPError as exc:
        body = exc.read(300).decode("utf-8", "replace")
        return False, f"HTTP {exc.code} {body[:160]}"
    except (URLError, TimeoutError, OSError) as exc:
        return False, str(exc)


def _pid_path() -> Path:
    return state_dir() / "opencode-bridge.pid"


def _log_path() -> Path:
    return state_dir() / "opencode-bridge.log"


def read_pid() -> int | None:
    try:
        value = int(_pid_path().read_text(encoding="ascii").strip())
    except (FileNotFoundError, ValueError, OSError):
        return None
    return value if value > 0 else None


def process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def write_pid(pid: int) -> None:
    path = _pid_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(f"{pid}\n", encoding="ascii")
    os.replace(temporary, path)


def clear_pid() -> None:
    try:
        _pid_path().unlink()
    except FileNotFoundError:
        pass


def run_ensure(*, timeout: float, strict: bool) -> bool:
    command = [sys.executable, "-m", "chatgpt_web2api", "ensure"]
    print("Reconciling the core ChatGPT-Web2API service...")
    try:
        completed = subprocess.run(command, check=False, timeout=timeout)
    except subprocess.TimeoutExpired:
        message = f"Core ensure timed out after {timeout:.0f}s"
        if strict:
            raise RuntimeError(message) from None
        print(f"Warning: {message}")
        return False
    if completed.returncode != 0:
        message = f"Core ensure exited with status {completed.returncode}"
        if strict:
            raise RuntimeError(message)
        print(f"Warning: {message}. Log in to ChatGPT in the managed Chrome window and retry.")
        return False
    return True


def _bridge_environment(state: dict[str, Any]) -> dict[str, str]:
    environment = os.environ.copy()
    environment["W2A_UPSTREAM"] = str(state["upstream_url"]).rstrip("/")
    parsed = urlparse(str(state["bridge_url"]))
    environment["W2A_OPENCODE_HOST"] = parsed.hostname or "127.0.0.1"
    if parsed.port:
        environment["W2A_OPENCODE_PORT"] = str(parsed.port)
    return environment


def start_background(state: dict[str, Any], *, wait_seconds: float) -> int:
    current = read_pid()
    root = bridge_root(str(state["bridge_url"]))
    healthy, detail = probe(f"{root}/health")
    if current and process_exists(current) and healthy:
        print(f"OpenCode bridge is already running (PID {current}; {detail}).")
        return 0
    if current and not process_exists(current):
        clear_pid()

    log_path = _log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_handle = log_path.open("ab", buffering=0)
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": log_handle,
        "stderr": subprocess.STDOUT,
        "env": _bridge_environment(state),
    }
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        )
        kwargs["close_fds"] = False
    else:
        kwargs["start_new_session"] = True
        kwargs["close_fds"] = True

    process = subprocess.Popen(
        [sys.executable, "-m", "chatgpt_web2api.opencode_bridge"],
        **kwargs,
    )
    log_handle.close()
    write_pid(process.pid)

    deadline = time.monotonic() + wait_seconds
    last_detail = "not checked"
    while time.monotonic() < deadline:
        if process.poll() is not None:
            clear_pid()
            print(f"Bridge exited with status {process.returncode}. See {log_path}.", file=sys.stderr)
            return 1
        healthy, last_detail = probe(f"{root}/health")
        if healthy:
            print(f"OpenCode bridge started (PID {process.pid}).")
            print(f"  URL: {state['bridge_url']}")
            print(f"  Log: {log_path}")
            return 0
        time.sleep(0.4)

    if process_exists(process.pid):
        print(f"Bridge process is running (PID {process.pid}), but health is not ready: {last_detail}")
        print(f"Check {log_path} and the upstream service.")
        return 0
    clear_pid()
    return 1


def start_foreground(state: dict[str, Any]) -> int:
    environment = _bridge_environment(state)
    print(f"Starting OpenCode bridge at {state['bridge_url']} (Ctrl+C to stop)...")
    completed = subprocess.run(
        [sys.executable, "-m", "chatgpt_web2api.opencode_bridge"],
        env=environment,
        check=False,
    )
    return completed.returncode


def stop_bridge(*, timeout: float = 10.0) -> int:
    pid = read_pid()
    if not pid:
        print("No managed OpenCode bridge PID file was found.")
        return 0
    if not process_exists(pid):
        clear_pid()
        print("Removed a stale OpenCode bridge PID file.")
        return 0

    print(f"Stopping OpenCode bridge PID {pid}...")
    if os.name == "nt":
        completed = subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if completed.returncode != 0 and process_exists(pid):
            return 1
    else:
        os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and process_exists(pid):
            time.sleep(0.2)
        if process_exists(pid):
            os.kill(pid, signal.SIGKILL)
    clear_pid()
    print("OpenCode bridge stopped.")
    return 0


def print_status(state: dict[str, Any]) -> int:
    upstream = str(state["upstream_url"]).rstrip("/")
    root = bridge_root(str(state["bridge_url"]))
    upstream_ok, upstream_detail = probe(f"{upstream}/health")
    bridge_ok, bridge_detail = probe(f"{root}/health")
    pid = read_pid()

    print(f"Core Web2API: {'ready' if upstream_ok else 'not ready'} — {upstream_detail}")
    process_label = "none"
    if pid:
        process_label = f"{pid} ({'alive' if process_exists(pid) else 'stale'})"
    print(f"OpenCode bridge: {'ready' if bridge_ok else 'not ready'} — {bridge_detail}")
    print(f"Managed bridge PID: {process_label}")
    print(f"OpenCode endpoint: {state['bridge_url']}")
    return 0 if upstream_ok and bridge_ok else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage the Web2API/OpenCode bridge stack.")
    parser.add_argument("--state", type=Path, default=default_state_path())
    subparsers = parser.add_subparsers(dest="command", required=True)

    start = subparsers.add_parser("start", help="Ensure Web2API and start the OpenCode bridge.")
    start.add_argument("--background", action="store_true")
    start.add_argument("--no-ensure", action="store_true")
    start.add_argument("--strict-ensure", action="store_true")
    start.add_argument("--ensure-timeout", type=float, default=120.0)
    start.add_argument("--wait-seconds", type=float, default=20.0)
    start.add_argument("--open-chatgpt", action="store_true")

    stop = subparsers.add_parser("stop", help="Stop the managed OpenCode bridge process.")
    stop.add_argument("--timeout", type=float, default=10.0)

    subparsers.add_parser("status", help="Probe the core service and bridge.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        state = load_state(args.state.expanduser())
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    if args.command == "stop":
        return stop_bridge(timeout=args.timeout)
    if args.command == "status":
        return print_status(state)

    upstream = str(state["upstream_url"])
    if not args.no_ensure and is_local_url(upstream):
        try:
            run_ensure(timeout=args.ensure_timeout, strict=args.strict_ensure)
        except RuntimeError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
    elif not is_local_url(upstream):
        print("Remote upstream detected; skipping local `chatgpt-web2api ensure`.")

    if args.open_chatgpt:
        webbrowser.open("https://chatgpt.com")

    if args.background:
        return start_background(state, wait_seconds=args.wait_seconds)
    return start_foreground(state)


if __name__ == "__main__":
    raise SystemExit(main())
