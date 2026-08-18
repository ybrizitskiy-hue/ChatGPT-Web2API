# OpenCode + ChatGPT-Web2API Tool Bridge

This fork contains an OpenAI-compatible adapter that lets OpenCode use a model selected in the ChatGPT web interface while OpenCode continues to execute local `read`, `write`, `edit`, `grep`, and shell tools.

> **Important:** this is a browser/UI bridge, not the official OpenAI API. It is inherently more brittle than a supported API integration. Keep OpenCode's tool permission prompts enabled, bind both services to loopback, and do not expose the ports to the internet.

## Architecture

```text
OpenCode Desktop / CLI
        |
        | OpenAI Chat Completions + tools
        v
OpenCode bridge       http://127.0.0.1:8010/v1
        |
        | strict text tool protocol + reconnect deduplication
        v
ChatGPT-Web2API       http://127.0.0.1:8000/v1
        |
        v
Chrome -> ChatGPT web -> selected model
```

The adapter sends OpenCode tool definitions to the model as a strict protocol, translates valid model requests into OpenAI `tool_calls`, feeds OpenCode tool results back on the next turn, emits OpenCode-compatible SSE, validates names and `tool_choice`, keeps an upstream turn alive after a client disconnect, and coalesces an identical retry for a bounded TTL.

The adapter supports **one tool call per model turn**. OpenCode may perform many sequential tool turns in one task.

## Быстрый запуск на Windows

Install Python 3.11+, Git for Windows, and OpenCode Desktop or CLI. Download or clone this fork, open `scripts`, and double-click:

```text
setup-opencode.cmd
```

Enter the Web2API URL (normally `http://127.0.0.1:8000`), its API key or blank when authentication is disabled, and select an exact model returned by `/v1/models`. The installer clones or updates the fork under `%LOCALAPPDATA%\ChatGPT-Web2API-src`, creates a virtual environment, installs the package, backs up and updates the global OpenCode config, creates desktop start/stop launchers, optionally runs Web2API `ensure`, opens ChatGPT for login, and starts the adapter.

The setup state file does **not** contain the API key. OpenCode needs the key in its provider configuration so it can send the Authorization header. The old OpenCode config is retained as a timestamped backup.

Windows command-line equivalent:

```powershell
cd ChatGPT-Web2API
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -U pip
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m chatgpt_web2api.opencode_setup
.\.venv\Scripts\python.exe -m chatgpt_web2api.opencode_stack start --background --open-chatgpt
```

## macOS / Linux

```bash
git clone https://github.com/ybrizitskiy-hue/ChatGPT-Web2API.git
cd ChatGPT-Web2API
./scripts/setup-opencode.sh
```

Manual equivalent:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -U pip
.venv/bin/python -m pip install -e .
.venv/bin/python -m chatgpt_web2api.opencode_setup
.venv/bin/python -m chatgpt_web2api.opencode_stack start --background --open-chatgpt
```

The default global OpenCode config is `~/.config/opencode/opencode.json`. Set `OPENCODE_CONFIG` or pass `--config PATH` to use another file.

## OpenCode configuration

The wizard preserves unrelated settings and adds a provider similar to:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "chatgpt-web": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "ChatGPT Web2API",
      "options": {
        "baseURL": "http://127.0.0.1:8010/v1",
        "apiKey": "YOUR_WEB2API_KEY_OR_local"
      },
      "models": {
        "YOUR_MODEL_SLUG": {
          "name": "ChatGPT Web · YOUR_MODEL_SLUG"
        }
      }
    }
  },
  "model": "chatgpt-web/YOUR_MODEL_SLUG"
}
```

Do not guess the model slug. Use an exact `id` returned by:

```bash
curl -H "Authorization: Bearer YOUR_KEY" http://127.0.0.1:8000/v1/models
```

Non-interactive setup:

```bash
W2A_API_KEY='your-key' \
python -m chatgpt_web2api.opencode_setup \
  --non-interactive \
  --upstream-url http://127.0.0.1:8000 \
  --model YOUR_MODEL_SLUG
```

Useful options:

```text
--bridge-url URL          adapter URL written into OpenCode
--provider-id ID          provider id, default chatgpt-web
--config PATH             OpenCode JSON/JSONC file
--state PATH              stack state file
--no-default-model        do not replace OpenCode's default model
--dry-run                 print a redacted merged config
--skip-model-probe        do not query Web2API during setup
```

JSONC comments and trailing commas are accepted. Rewrites are valid formatted JSON; the original remains as a timestamped backup.

## Start, stop, status

```bash
python -m chatgpt_web2api.opencode_stack start --background --open-chatgpt
python -m chatgpt_web2api.opencode_stack status
python -m chatgpt_web2api.opencode_stack stop
```

For a remote upstream, local `ensure` is skipped automatically. It may also be disabled with `--no-ensure`.

State, PID, and log locations:

- Windows: `%LOCALAPPDATA%\ChatGPT-Web2API`
- macOS/Linux: `~/.config/chatgpt-web2api`

Environment variables:

```text
W2A_UPSTREAM                 original Web2API base URL
W2A_OPENCODE_HOST            adapter bind host; keep 127.0.0.1
W2A_OPENCODE_PORT            adapter port, default 8010
W2A_OPENCODE_TIMEOUT         upstream timeout, default 930 seconds
W2A_OPENCODE_CACHE_TTL       successful replay TTL, default 300 seconds
W2A_OPENCODE_STATE_DIR       state/log/PID directory
OPENCODE_CONFIG              OpenCode config path
```

## Verification

Check health:

```bash
python -m chatgpt_web2api.opencode_stack status
```

Query models through the adapter:

```bash
curl -H "Authorization: Bearer YOUR_KEY" http://127.0.0.1:8010/v1/models
```

Plain chat smoke test:

```bash
curl http://127.0.0.1:8010/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer YOUR_KEY' \
  -d '{
    "model": "YOUR_MODEL_SLUG",
    "stream": false,
    "messages": [{"role": "user", "content": "Reply with exactly: bridge-ok"}]
  }'
```

OpenCode tool test in a disposable directory:

```text
Read sample.txt with a tool, report the first line, then create result.txt with that line. Do not guess file contents.
```

Approve only the expected read/write operations and verify the file contents.

## Reconnect guarantees

While the adapter process remains alive, an identical retry joins an existing in-flight task, a completed successful response is replayed for the configured TTL, and cancellation of one client does not cancel the shared upstream task. Authentication failures, rate limits, and server failures are not retained as successful replay results.

There is **no guarantee of recovery when the adapter process, Chrome, or the entire machine crashes during a generation**. The answer can finish in ChatGPT while OpenCode never receives it, and a blind retry can duplicate a turn. Inspect the ChatGPT conversation and adapter log before retrying a non-idempotent action.

OpenCode's local session and the ChatGPT web conversation are separate state. The adapter forwards full OpenCode history but is not a universal resume token for an interrupted ChatGPT generation.

## Tool and security model

- Only a function name advertised by OpenCode in the current request is accepted.
- Arguments must be a JSON object.
- `tool_choice=none`, `required`, and named tools are enforced.
- Unknown or malformed requests become protocol errors and are not executed.
- The bridge executes no shell command itself. OpenCode remains the local executor and permission boundary.
- Tool calling is a strict text protocol because ChatGPT Web does not expose the official API tool channel used by OpenCode.

Keep ports on `127.0.0.1`, keep OpenCode approvals enabled, test in a disposable repository, never publish keys/cookies/session tokens, and do not treat browser automation as a production SLA or an official API.

## Troubleshooting

**Wrong model or model not found:** inspect live `/v1/models`, rerun setup with `--model`, and restart OpenCode.

**401:** the OpenCode provider key must match Web2API. If authentication is disabled, use `local`.

**Health works but requests fail:** log in to ChatGPT in the managed browser, dismiss modals, verify model access, and rerun the stack start command.

**Malformed tool-call:** the model failed the strict JSON envelope or requested an unavailable tool. Retry once with a clearer task; do not weaken validation.

**Timeout but answer appears in ChatGPT:** do not immediately retry a write or shell action. Inspect OpenCode, the adapter log, and the ChatGPT conversation.

**Port conflict:** start with `W2A_OPENCODE_PORT=8011` and rerun setup using `--bridge-url http://127.0.0.1:8011/v1`.

## Updating and uninstalling

Rerun `scripts/setup-opencode.cmd` on Windows, or:

```bash
git pull --ff-only
.venv/bin/python -m pip install -e .
```

To uninstall, stop the adapter, remove the `chatgpt-web` provider or restore the backup, and delete the source/state directories. Browser login remains until you log out or remove that profile.

## Maintainer tests

```bash
python -m pip install -e '.[dev]'
ruff check src/ tests/
pytest -v -m 'not e2e'
python -m build
```

See [`docs/OPENCODE_HANDOVER.md`](docs/OPENCODE_HANDOVER.md) for a complete handover that can be pasted into Codex to perform installation and acceptance testing on another machine.
