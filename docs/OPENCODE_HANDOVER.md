# Codex Handover: Install and Validate OpenCode + ChatGPT-Web2API

Use this document as the complete execution brief for a Codex agent working on the user's machine. Do not ask the user to repeat information that can be detected locally. Never print, commit, or paste API keys, ChatGPT cookies, or session tokens.

## Objective

Install the user's fork of ChatGPT-Web2API, configure OpenCode to use the local OpenCode tool bridge, start the services, guide the user through ChatGPT login, and prove the complete tool loop and reconnect behavior with disposable files.

```text
Repository: https://github.com/ybrizitskiy-hue/ChatGPT-Web2API.git
Branch after merge: master
Core Web2API: http://127.0.0.1:8000
OpenCode bridge: http://127.0.0.1:8010/v1
```

## Non-negotiable safety rules

1. Bind services to loopback unless the user explicitly requests a secured remote deployment.
2. Preserve unrelated OpenCode configuration and verify a timestamped backup before accepting a rewrite.
3. Keep OpenCode write/shell/network approval prompts enabled.
4. Use a disposable directory for acceptance tests.
5. Never retry a timed-out write or shell turn blindly; inspect ChatGPT and the bridge log first.
6. Do not claim success without exact pass/fail results for each acceptance check.

## Phase 1 — Inspect the machine

Collect without exposing secrets:

```bash
git --version
python --version
python3 --version
opencode --version
```

On Windows:

```powershell
py -0p
Get-Command git, py, python, opencode -ErrorAction SilentlyContinue
```

Require Python 3.11+ and Git. If OpenCode Desktop is installed but the CLI is not on PATH, continue configuration and note that end-to-end validation must be performed in the app.

Resolve the OpenCode config in this order: `$OPENCODE_CONFIG`, `~/.config/opencode/opencode.json`, `~/.config/opencode/opencode.jsonc`, or create `opencode.json`. Do not manually rewrite JSONC; use the setup helper.

## Phase 2 — Install or update

Windows preferred path:

```powershell
.\scripts\setup-opencode.cmd
```

Scripted Windows path:

```powershell
$env:W2A_API_KEY = '<provided securely>'
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -U pip
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m chatgpt_web2api.opencode_setup `
  --non-interactive `
  --upstream-url http://127.0.0.1:8000 `
  --model '<exact model id>'
Remove-Item Env:W2A_API_KEY
```

macOS/Linux preferred path:

```bash
./scripts/setup-opencode.sh
```

Manual:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -U pip
.venv/bin/python -m pip install -e .
W2A_API_KEY='<provided securely>' \
.venv/bin/python -m chatgpt_web2api.opencode_setup \
  --non-interactive \
  --upstream-url http://127.0.0.1:8000 \
  --model '<exact model id>'
unset W2A_API_KEY
```

When the model is unknown, start the core service and query:

```bash
curl -H 'Authorization: Bearer <key>' http://127.0.0.1:8000/v1/models
```

Use an exact returned `id`; never invent a Sol/reasoning slug.

## Phase 3 — Audit generated configuration

Confirm the OpenCode config contains:

```text
provider.chatgpt-web.npm = @ai-sdk/openai-compatible
provider.chatgpt-web.options.baseURL = http://127.0.0.1:8010/v1
provider.chatgpt-web.options.apiKey = <configured key or local>
provider.chatgpt-web.models.<selected model>
```

Confirm unrelated settings remain, a `bak.<timestamp>` file exists, and the stack state contains URL/model/path metadata but no API key. Never include the key value in the report.

## Phase 4 — Start and authenticate

```bash
python -m chatgpt_web2api.opencode_stack start --background --open-chatgpt
python -m chatgpt_web2api.opencode_stack status
```

Use the virtual-environment Python path when appropriate. The start command should run core `ensure` for local deployments, open ChatGPT, start the bridge, and save a PID/log. Ask the user to complete login only when the browser is unauthenticated. Never request their password, MFA code, cookies, or session token.

Both core and bridge must be ready before continuing.

## Phase 5 — Repository tests

```bash
python -m pip install -e '.[dev]'
ruff check src/ tests/
python -m compileall -q src
pytest -v -m 'not e2e'
python -m pip check
python -m build
```

Every command must exit zero. Record test count and versions. On failure, capture the smallest relevant trace and fix the root cause.

## Phase 6 — Wire-level smoke tests

Health and models:

```bash
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8010/health
curl -H 'Authorization: Bearer <key>' http://127.0.0.1:8010/v1/models
```

Send a non-streaming plain request through port 8010 and require the exact text `bridge-ok`.

For a synthetic harmless tool such as `read_test_file(path: string)`, validate the non-streaming response has:

```text
choices[0].finish_reason = tool_calls
choices[0].message.tool_calls[0].type = function
function.name = exact advertised name
function.arguments = valid JSON object text
```

For streaming validate SSE `data:` framing, `tool_calls[0].index`, tool id/name, a terminal `finish_reason: tool_calls`, and `[DONE]`. Do not execute a shell tool during this protocol-only check.

## Phase 7 — OpenCode end-to-end loop

Create a disposable directory with `sample.txt` whose first line is `ACCEPTANCE-READ-OK`. Open it in OpenCode and prompt:

```text
Use a file-reading tool to read sample.txt. Then use a file-writing tool to create result.txt containing exactly the first line. Do not infer or invent the contents. Stop after verifying result.txt.
```

Approve only expected operations. Pass criteria:

1. model requests a read tool;
2. OpenCode executes it locally;
3. result returns to the model;
4. model requests a write tool;
5. `result.txt` equals `ACCEPTANCE-READ-OK`;
6. final response claims no extra changes.

Delete the disposable directory afterward.

## Phase 8 — Disconnect/retry test

This tests only a client connection loss while bridge/core/Chrome remain alive.

1. Start a harmless long request.
2. Interrupt the OpenCode client after the request reaches the bridge.
3. Keep bridge/core/Chrome running.
4. Reconnect and retry the identical request within the replay TTL.
5. Inspect the bridge log and ChatGPT conversation.

Pass only when one upstream turn was sent, the retry joins/replays it, OpenCode receives a valid result, and no local action is duplicated.

Explicitly state that this does not prove recovery from a bridge-process, Chrome, or machine crash. In-flight crash recovery is not guaranteed.

## Final report

Return:

```text
OS and Python version
Fork commit SHA
OpenCode version/app build
Core URL and bridge URL
Selected model slug
Config path and backup path
Repository test results
Health/model/plain/tool/OpenCode/disconnect results
Known limitations
Exact start/stop commands or launcher paths
```

Redact the API key and all ChatGPT authentication material.

## Known limitations that must not be weakened

- Tool calls use a validated text protocol, not native ChatGPT Web tool calls.
- One tool call is supported per model turn; sequential loops are supported.
- Client reconnect recovery requires the bridge process to remain alive.
- A bridge/core/Chrome/machine crash can leave an ambiguous upstream result and duplicate a blind retry.
- ChatGPT account usage limits apply; this is not an official API quota bypass.
- Browser DOM/backend changes can break automation.

Do not claim production-grade exactly-once execution.
