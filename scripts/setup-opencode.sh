#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
BASE=${XDG_DATA_HOME:-"$HOME/.local/share"}/chatgpt-web2api-opencode
VENV="$BASE/venv"

if command -v python3 >/dev/null 2>&1; then
  PYTHON=python3
elif command -v python >/dev/null 2>&1; then
  PYTHON=python
else
  echo "Python 3.11 or newer is required." >&2
  exit 1
fi

"$PYTHON" -c 'import sys; print("Using Python", ".".join(map(str, sys.version_info[:3]))); raise SystemExit(sys.version_info < (3, 11))'
mkdir -p "$BASE"
if [ ! -x "$VENV/bin/python" ]; then
  "$PYTHON" -m venv "$VENV"
fi
VPY="$VENV/bin/python"
"$VPY" -m pip install --upgrade pip

if [ -f "$ROOT/pyproject.toml" ]; then
  "$VPY" -m pip install -e "$ROOT"
else
  "$VPY" -m pip install 'https://github.com/ybrizitskiy-hue/ChatGPT-Web2API/archive/refs/heads/master.zip'
fi

UPSTREAM=${W2A_UPSTREAM:-http://127.0.0.1:8080}
BRIDGE_URL=${W2A_BRIDGE_URL:-http://127.0.0.1:8010/v1}
MODEL=${W2A_MODEL:-auto}
if [ -n "${W2A_API_KEY:-}" ]; then
  export W2A_OPENCODE_API_KEY=$W2A_API_KEY
fi

set -- -m chatgpt_web2api.opencode_setup setup \
  --non-interactive \
  --upstream "$UPSTREAM" \
  --bridge-url "$BRIDGE_URL" \
  --model "$MODEL"
if [ "${W2A_SET_DEFAULT:-1}" != "0" ]; then
  set -- "$@" --set-default
fi
if [ "${W2A_CONFIGURE_ONLY:-0}" != "1" ]; then
  set -- "$@" --start
fi
exec "$VPY" "$@"
