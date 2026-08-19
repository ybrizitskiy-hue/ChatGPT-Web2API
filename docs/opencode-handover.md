# Handover: OpenCode + ChatGPT-Web2API

Use this document when handing the integration to Codex or another coding agent on the target computer. The objective is to install/update, verify, diagnose, or maintain the integration **without exposing authentication material and without reintroducing browser monkey-patches**.

## Current verified state

Live acceptance was completed on Windows with a real ChatGPT account on **2026-08-19**.

Verified:

- stock/direct OpenCode chat returns a completed ChatGPT Web answer;
- tools-sidecar chat returns answers;
- a direct `tool_choice=required` HTTP probe returns a standard OpenAI `tool_calls` payload;
- OpenCode executes `bash` through the sidecar;
- sequential `bash -> read -> final answer` works;
- `create -> edit -> read -> delete/cleanup` works and returns `TOOLS_FULLY_WORKING`;
- false model claims that the OpenCode filesystem/terminal is unavailable are handled by one bounded sidecar-only corrective retry.

Do not treat an unrun GitHub Actions workflow as a passing CI result. During development of this fork, GitHub Actions repeatedly created no workflow run for PR heads. Local/targeted tests and the live Windows acceptance above are the evidence available unless Actions starts working later.

## Target layout

```text
Repository: https://github.com/ybrizitskiy-hue/ChatGPT-Web2API.git
Branch: master
Web2API core:       http://127.0.0.1:8080
Tools sidecar:      http://127.0.0.1:8010/v1
Main OpenCode provider:       chatgpt-web
Diagnostic OpenCode provider: chatgpt-web-direct
```

Normal path:

```text
OpenCode
  -> chatgpt-web / 8010
  -> OpenCode tools sidecar
  -> stock Web2API / 8080
  -> dedicated Chrome / ChatGPT Web
```

Diagnostic path:

```text
OpenCode
  -> chatgpt-web-direct / 8080
  -> stock Web2API
  -> dedicated Chrome / ChatGPT Web
```

OpenCode executes local tools. ChatGPT Web only requests offered functions.

## Non-negotiable maintenance rule

**Do not patch Chrome navigation, composer input, DOM selectors, or CDP command acknowledgement in order to fix a sidecar/tool-protocol problem.**

A previous iteration did this and caused regressions. The correct debugging order is:

1. prove `chatgpt-web-direct` returns a normal answer;
2. if direct works, keep the Web2API browser transport unchanged and debug port 8010/tool translation;
3. only inspect Web2API core if direct also fails.

The current architecture deliberately keeps the OpenCode adapter external to the browser transport.

## Security / secrets

- Keep Web2API and the sidecar on loopback for normal local use.
- Never print or transmit the ChatGPT password, MFA code, browser cookies, session token, or the generated local API key.
- The key is stored at `~/.chatgpt-web2api/opencode-api-key` and referenced by OpenCode through `{file:...}`.
- Preserve existing OpenCode permission rules. When no policy exists, the setup default asks before edits, shell commands, and external-directory access.
- Use disposable files for write tests and clean them up.
- After a bridge/host crash, never blindly retry a write-capable logical turn; inspect the browser conversation and working tree first.

## Windows installation/update

Preferred path:

```text
scripts\setup-opencode.cmd
```

Expected behavior:

1. detect Git;
2. install Git if absent (`winget`, then MinGit fallback);
3. detect Python 3.11-3.13;
4. install Python 3.13 if needed (`winget`, then signed python.org installer fallback);
5. clone/update `master` under `%LOCALAPPDATA%\ChatGPT-Web2API-OpenCode`;
6. create/use an isolated venv;
7. install/update the package from the managed checkout;
8. generate/reuse a local service key;
9. merge OpenCode config and reference the key file without embedding the secret;
10. configure `chatgpt-web` at `http://127.0.0.1:8010/v1` with tools enabled;
11. configure `chatgpt-web-direct` at `http://127.0.0.1:8080/v1` with tools disabled;
12. make `chatgpt-web/<model>` the default when setup uses `--set-default`;
13. create reusable launchers and start the stack.

No OpenAI API key is required for a local installation.

The human-only account step is signing in to ChatGPT in the dedicated Chrome profile opened by Web2API.

Running the installer again is the supported updater.

## macOS/Linux

Git and Python 3.11+ are prerequisites for the shell bootstrap:

```bash
git clone https://github.com/ybrizitskiy-hue/ChatGPT-Web2API.git
cd ChatGPT-Web2API
chmod +x scripts/setup-opencode.sh
./scripts/setup-opencode.sh
```

Do not assume the shell bootstrap installs OS prerequisites automatically; that behavior is currently specific to Windows.

## Configuration audit

Confirm these exist after local setup:

```text
~/.chatgpt-web2api/config.json
~/.chatgpt-web2api/opencode-api-key
~/.config/opencode/opencode.json
~/.chatgpt-web2api/start-opencode-web2api.cmd
~/.chatgpt-web2api/start-opencode-web2api.sh
```

Confirm OpenCode has both providers:

```text
chatgpt-web:
  npm = @ai-sdk/openai-compatible
  baseURL = http://127.0.0.1:8010/v1
  tool_call = true

chatgpt-web-direct:
  npm = @ai-sdk/openai-compatible
  baseURL = http://127.0.0.1:8080/v1
  tool_call = false
```

When setup was run with `--set-default`, confirm:

```text
model = chatgpt-web/<selected model>
```

Do not print the key referenced by `apiKey = {file:...}`.

## Start / doctor

Use the generated launcher or:

```bash
chatgpt-web2api-opencode start
```

Then:

```bash
chatgpt-web2api-opencode doctor
```

On Windows from a checkout, the one-click diagnostic wrapper is:

```text
scripts\run-opencode-diagnostics.cmd
```

Expected doctor checks:

```text
OK  Web2API config
OK  API key file
OK  OpenCode provider
OK  Web2API health
OK  Web2API authentication
OK  Bridge health
OK  Model catalog
```

A `degraded` or `broken` Web2API health payload is not ready.

## Model selection

Start with `auto` unless an exact model identifier was verified from the live model catalog. Never invent a Sol/reasoning model slug.

On the live validation account, `gpt-5.6-sol-wm` appeared in `/v1/models` on 2026-08-19, but model identifiers are not treated as permanent constants.

To pin an exact current model:

```bash
chatgpt-web2api-opencode setup --model EXACT_MODEL_ID --set-default --non-interactive
```

## Required acceptance sequence

Use a disposable project/folder and a **new OpenCode session** when switching providers.

### A. Direct baseline

Select:

```text
chatgpt-web-direct/auto
```

Send:

```text
Hi
```

Pass: ChatGPT's completed answer appears back in OpenCode. Merely seeing an answer in the managed browser is not a pass.

If direct fails, stop and debug stock Web2API response/turn reconciliation. Do not blame the tools sidecar.

### B. Main tools provider / safe shell

Select:

```text
chatgpt-web/auto
```

Send:

```text
Use the bash tool. Run exactly: echo OPENCODE_TOOL_OK
You MUST execute the tool; do not answer from memory.
After the tool completes, return only its stdout.
```

Expected final answer:

```text
OPENCODE_TOOL_OK
```

This proves model -> sidecar -> OpenAI tool_call -> OpenCode local execution -> tool result -> model -> final answer.

### C. Sequential loop

```text
Use the bash tool to create a file named opencode_tool_test.txt containing exactly:

tool-loop-ok

Then use the read tool to read opencode_tool_test.txt back.
Return only the exact contents of the file.
Do not modify any other file.
```

Expected:

```text
tool-loop-ok
```

### D. File create/edit/read/cleanup

```text
Create a file named opencode_edit_test.txt with exactly this content:

version-1

Then use the appropriate file editing tool to change it to:

version-2

Read the file back and verify that its exact contents are version-2.

Finally delete opencode_edit_test.txt and also delete opencode_tool_test.txt from the previous test.

Return only:
TOOLS_FULLY_WORKING

Do not modify any other files.
```

Expected:

```text
TOOLS_FULLY_WORKING
```

Confirm no disposable files remain.

## Direct HTTP tool-protocol probe

This isolates the sidecar from OpenCode's UI/tool executor. On Windows PowerShell:

```powershell
$key = (Get-Content "$HOME\.chatgpt-web2api\opencode-api-key" -Raw).Trim()

$body = @{
  model = "auto"
  stream = $false
  messages = @(
    @{ role = "user"; content = "You must call the echo_probe tool with text exactly bridge-tool-ok." }
  )
  tools = @(
    @{
      type = "function"
      function = @{
        name = "echo_probe"
        description = "Echo a text value"
        parameters = @{
          type = "object"
          properties = @{ text = @{ type = "string" } }
          required = @("text")
        }
      }
    }
  )
  tool_choice = "required"
} | ConvertTo-Json -Depth 20

Invoke-RestMethod `
  -Uri "http://127.0.0.1:8010/v1/chat/completions" `
  -Method POST `
  -Headers @{ Authorization = "Bearer $key" } `
  -ContentType "application/json" `
  -Body $body | ConvertTo-Json -Depth 20
```

Pass criteria:

```text
finish_reason = tool_calls
function.name = echo_probe
arguments = {"text":"bridge-tool-ok"}
```

Do not paste the example JSON response back into PowerShell as a command.

## Agentic false-denial recovery

A web model can occasionally answer something equivalent to:

```text
I can't access or modify the OpenCode filesystem from this environment.
```

while OpenCode has actually supplied local tools.

The sidecar's recovery is deliberately bounded:

- it only applies when tools are present;
- it only applies to an explicit local-action request plus a false filesystem/terminal access denial;
- it does not run for `tool_choice=none`;
- it retries once;
- the corrective retry uses `tool_choice=required`.

Do not turn this into a generic retry loop. Ordinary conversational questions must remain able to answer without a tool.

## Reconnect acceptance

Not yet live-proven on the user's machine after the final core fixes. Treat this as an additional maintenance test, not as already-verified behavior.

Use a non-destructive long request. Interrupt the OpenCode client connection while keeping the bridge and Web2API processes alive, then repeat the identical logical request.

Desired behavior:

- downstream cancellation does not cancel the shared upstream task;
- an identical retry joins the in-flight task or receives the short replayed success;
- client reconnect alone does not cause a second browser SEND.

This protection is in-memory only. A bridge/host restart loses in-flight/replay state.

## Development validation

For changes affecting this integration, run as much of the following as the environment supports:

```bash
python -m compileall -q src tests
ruff check src tests
pytest -v -m "not e2e"
```

Windows installer gate:

```powershell
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File scripts/setup-opencode.ps1 -DryRun
```

Also verify that `scripts/setup-opencode.cmd` and `scripts/run-opencode-diagnostics.cmd` parse/run on Windows.

A real-account smoke test is separate from CI.

## Logs / debugging decision tree

### ChatGPT answered, OpenCode still waits on direct provider

Inspect stock Web2API logs. Important stages include:

```text
Message sent
Assistant message appeared
Resolved conversation id
Backend end_turn=true or accepted fresh-chat completion
Conversation: <id>
```

If the browser answer is complete but the request later dies in turn reconciliation, debug `completion_detector` / `turn_anchor`. Keep the fix narrowly scoped and covered by a regression test.

### Direct works, tools provider fails

Do not modify Chrome/CDP. Inspect:

- whether OpenCode sent `tools`;
- tool-choice mode;
- sentinel/tool envelope parsing;
- OpenAI `tool_calls` SSE shape;
- the tool-result message that comes back from OpenCode;
- bounded false-denial recovery.

### File requested but tool says unavailable

Confirm the file actually exists in the project/workspace OpenCode opened. A request for `README.md` is not a valid workspace-independent smoke test.

## Known limitations to preserve in docs

- One model-requested tool call per model turn; sequential tool loops work.
- Browser turns are buffered rather than token-streamed end to end.
- In-flight/replay state is memory-only.
- OpenCode auxiliary/title/tool turns may create several visible ChatGPT conversations in the managed account; sidebar chat clutter is expected with the current stateless full-history approach.
- Long agent sessions resend history and tool schemas and may hit context limits.
- Remote/non-loopback deployment needs independent TLS/auth/firewall/security work.

## Final report format

Return concrete evidence, not assumptions:

```text
RESULT: PASS / PARTIAL / FAIL
OS:
Python:
Git:
OpenCode:
Repository commit:
Web2API doctor: PASS/FAIL
Direct provider chat: PASS/FAIL
Tools provider chat: PASS/FAIL
HTTP required-tool probe: PASS/FAIL/NOT RUN
Bash tool: PASS/FAIL/NOT RUN
Sequential bash->read loop: PASS/FAIL/NOT RUN
Write/edit/read/cleanup loop: PASS/FAIL/NOT RUN
Reconnect test: PASS/FAIL/NOT RUN
Selected model: <verified model id or auto>
Notes:
```

Never include secret values in the report.
