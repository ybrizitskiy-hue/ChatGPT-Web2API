"""CLI setup and launcher for the OpenCode compatibility bridge."""

from __future__ import annotations

import argparse
import getpass
import os
import secrets
from pathlib import Path

from .opencode_baseline import (
    DIRECT_PROVIDER,
    configure_direct_provider,
    restore_stock_core_tuning,
)
from .opencode_setup_common import (
    DEFAULT_BRIDGE_URL,
    DEFAULT_PROVIDER,
    DEFAULT_TIMEOUT,
    DEFAULT_UPSTREAM,
    choose_model,
    configure_core,
    configure_opencode,
    core_config_path,
    global_opencode_config_path,
    is_loopback,
    key_file_path,
    model_catalog,
    normalize_url,
    project_config_path,
    read_key,
    state_dir,
    write_launchers,
    write_secret,
)
from .opencode_setup_runtime import doctor, serve, start


def setup(args: argparse.Namespace) -> int:
    upstream = normalize_url(args.upstream, v1=False)
    bridge_url = normalize_url(args.bridge_url, v1=True)
    local_core = is_loopback(upstream)
    local_bridge = is_loopback(bridge_url)
    key_file = args.key_file.expanduser()
    api_key = args.api_key or os.environ.get("W2A_OPENCODE_API_KEY") or read_key(key_file)
    if not api_key and not args.non_interactive:
        label = "Local API key (blank generates one)" if local_core else "Remote Web2API API key"
        api_key = getpass.getpass(f"{label}: ").strip()
    if not api_key and local_core:
        api_key = secrets.token_urlsafe(32)
    if not api_key:
        print("A remote Web2API endpoint requires its existing API key.")
        return 2

    models = model_catalog(upstream, api_key)
    model = args.model or choose_model(models)
    if not args.model and not args.non_interactive:
        if models:
            print("Models visible through Web2API:")
            for item in models:
                print(f"  - {item}")
        entered = input(f"Model id [{model}]: ").strip()
        model = entered or model
    model_name = args.model_name or (
        "ChatGPT Web Sol"
        if "sol" in model.lower()
        else "ChatGPT Web (active browser model)"
        if model == "auto"
        else f"ChatGPT Web ({model})"
    )

    project_dir = args.project_dir.expanduser().resolve()
    opencode_path = args.opencode_config or (
        global_opencode_config_path()
        if args.scope == "global"
        else project_config_path(project_dir)
    )
    core_path = args.core_config.expanduser()
    write_secret(key_file, api_key)
    core_backup = None
    if local_core:
        core_backup = configure_core(core_path, api_key, upstream, model)
        # Older versions of our OpenCode installer changed Web2API's browser
        # completion detector budgets. Undo only the exact values we injected
        # so the core again runs with its stock defaults. User custom values
        # that differ from ours are preserved.
        restore_stock_core_tuning(core_path)
    opencode_backup = configure_opencode(
        opencode_path.expanduser(),
        provider_id=args.provider_id,
        bridge_url=bridge_url,
        key_file=key_file,
        model=model,
        model_name=model_name,
        set_default=args.set_default,
        safe_permissions=not args.no_safe_permissions,
    )
    # Keep a direct provider beside the production tool sidecar as an A/B
    # diagnostic route. The sidecar has passed live chat, bash, read,
    # write/edit/read/cleanup and multi-turn tool-loop acceptance, so setup no
    # longer makes the direct baseline the default.
    configure_direct_provider(
        opencode_path.expanduser(),
        upstream=upstream,
        key_file=key_file,
        model=model,
        model_name=model_name,
        set_default=False,
    )
    cmd, sh = write_launchers(
        state_dir(),
        config=core_path,
        upstream=upstream,
        bridge_url=bridge_url,
        start_core=local_core,
        start_bridge=local_bridge,
    )

    print("OpenCode integration configured.")
    print(f"  OpenCode config: {opencode_path}")
    print(f"  API key file: {key_file}")
    print(f"  Tools provider: {args.provider_id}/{model}")
    print(f"  Direct diagnostic baseline: {DIRECT_PROVIDER}/{model}")
    print(f"  Windows launcher: {cmd}")
    print(f"  macOS/Linux launcher: {sh}")
    if core_backup:
        print(f"  Web2API backup: {core_backup}")
    if opencode_backup:
        print(f"  OpenCode backup: {opencode_backup}")
    if local_core:
        print("Sign in to ChatGPT in the dedicated Chrome window when it opens.")
    if args.start:
        return start(
            argparse.Namespace(
                config=core_path,
                upstream=upstream,
                bridge_url=bridge_url,
                key_file=key_file,
                no_core=not local_core,
                no_bridge=not local_bridge,
                launch_opencode=False,
                startup_timeout=600.0,
            )
        )
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="chatgpt-web2api-opencode",
        description="OpenCode bridge setup, launcher, and diagnostics",
    )
    sub = root.add_subparsers(dest="command")

    serve_parser = sub.add_parser("serve", help="Run the OpenCode compatibility bridge")
    serve_parser.add_argument("--upstream", default=DEFAULT_UPSTREAM)
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8010)
    serve_parser.add_argument("--cache-ttl", type=float, default=60)
    serve_parser.add_argument("--cache-max-entries", type=int, default=256)
    serve_parser.add_argument("--heartbeat-interval", type=float, default=10)
    serve_parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    serve_parser.add_argument("--api-key-file", type=Path, default=key_file_path())

    setup_parser = sub.add_parser("setup", help="Configure Web2API and OpenCode")
    setup_parser.add_argument("--api-key")
    setup_parser.add_argument("--key-file", type=Path, default=key_file_path())
    setup_parser.add_argument("--upstream", default=DEFAULT_UPSTREAM)
    setup_parser.add_argument("--bridge-url", default=DEFAULT_BRIDGE_URL)
    setup_parser.add_argument("--provider-id", default=DEFAULT_PROVIDER)
    setup_parser.add_argument("--model")
    setup_parser.add_argument("--model-name")
    setup_parser.add_argument("--scope", choices=["global", "project"], default="global")
    setup_parser.add_argument("--project-dir", type=Path, default=Path.cwd())
    setup_parser.add_argument("--opencode-config", type=Path)
    setup_parser.add_argument("--core-config", type=Path, default=core_config_path())
    setup_parser.add_argument("--set-default", action="store_true")
    setup_parser.add_argument("--no-safe-permissions", action="store_true")
    setup_parser.add_argument("--non-interactive", action="store_true")
    setup_parser.add_argument("--start", action="store_true")

    start_parser = sub.add_parser("start", help="Start or verify the configured local/remote stack")
    start_parser.add_argument("--config", type=Path, default=core_config_path())
    start_parser.add_argument("--upstream", default=DEFAULT_UPSTREAM)
    start_parser.add_argument("--bridge-url", default=DEFAULT_BRIDGE_URL)
    start_parser.add_argument(
        "--api-key-file", dest="key_file", type=Path, default=key_file_path()
    )
    start_parser.add_argument("--no-core", action="store_true")
    start_parser.add_argument("--no-bridge", action="store_true")
    start_parser.add_argument("--launch-opencode", action="store_true")
    start_parser.add_argument("--startup-timeout", type=float, default=600)

    doctor_parser = sub.add_parser(
        "doctor", help="Verify configuration, authentication, bridge, and models"
    )
    doctor_parser.add_argument("--core-config", type=Path, default=core_config_path())
    doctor_parser.add_argument("--upstream", default=DEFAULT_UPSTREAM)
    doctor_parser.add_argument("--bridge-url", default=DEFAULT_BRIDGE_URL)
    doctor_parser.add_argument("--key-file", type=Path, default=key_file_path())
    doctor_parser.add_argument("--provider-id", default=DEFAULT_PROVIDER)
    doctor_parser.add_argument("--scope", choices=["global", "project"], default="global")
    doctor_parser.add_argument("--project-dir", type=Path, default=Path.cwd())
    doctor_parser.add_argument("--opencode-config", type=Path)
    return root


def main() -> None:
    root = parser()
    args = root.parse_args()
    command = args.command or "serve"
    if command == "serve":
        if args.command is None:
            args = root.parse_args(["serve"])
        code = serve(args)
    elif command == "setup":
        code = setup(args)
    elif command == "start":
        code = start(args)
    elif command == "doctor":
        code = doctor(args)
    else:
        root.error(f"Unknown command: {command}")
        return
    raise SystemExit(code)


if __name__ == "__main__":
    main()
