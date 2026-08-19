# OpenCode + ChatGPT-Web2API

This fork lets OpenCode use a logged-in ChatGPT Web account as its model while **OpenCode executes local coding tools** such as `bash`, `read`, `write`, `edit`, `grep`, and other function tools.

> This is an unofficial browser bridge, not the official OpenAI API. Keep it on loopback unless you deliberately secure a remote deployment. ChatGPT Web and OpenCode can change independently, so browser/protocol maintenance may occasionally be required.

## Current status

The Windows path was live-tested against a real ChatGPT account on **2026-08-19**. The following acceptance flows passed:

- ordinary chat response returned from ChatGPT Web to OpenCode;
- direct `tool_choice=required` wire probe returned a valid OpenAI `tool_calls` response;
- OpenCode executed a `bash` tool call and returned the result to the model;
- a sequential `bash -> read -> final answer` tool loop completed;
- a `create -> edit -> read -> delete/cleanup` file workflow completed and returned `TOOLS_FULLY_WORKING`;
- the main tools provider and the direct no-tools baseline both return model responses.

The browser/CDP core is intentionally kept separate from the OpenCode adapter. The tools layer lives in the sidecar on port 8010.

## Architecture

```text
OpenCode Desktop / CLI
        |
        | OpenAI-compatible Chat Completions + tool_calls
        v
OpenCode tools bridge   http://127.0.0.1:8010/v1
        |
        | validated text tool protocol + retry/replay protection
        v
ChatGPT-Web2API         http://127.0.0.1:8080/v1
        |
        | Chrome DevTools Protocol
        v
Dedicated Chrome profile -> chatgpt.com -> selected ChatGPT model
```

OpenCode executes tools locally. ChatGPT Web only decides which offered function should be called. The bridge validates the requested function name and arguments, returns an OpenAI-compatible `tool_calls` response, and feeds the subsequent OpenCode tool result back to ChatGPT on the next model turn.

Two OpenCode providers are installed:

```text
chatgpt-web         -> 127.0.0.1:8010/v1 -> tools enabled (normal use)
chatgpt-web-direct  -> 127.0.0.1:8080/v1 -> tools disabled (diagnostic baseline)
```

`chatgpt-web` is the normal/default provider. Use `chatgpt-web-direct` only to answer the diagnostic question: "Does stock Web2API return a normal response without the tools sidecar?"

## Windows: one-click install/update

OpenCode itself should already be installed. For the bridge, double-click:

```text
scripts\setup-opencode.cmd
```

The installer is update-safe and performs the local setup automatically:

1. detects Git and installs it when missing (`winget`, then MinGit fallback);
2. detects Python 3.11-3.13 and installs Python 3.13 when needed (`winget`, then signed python.org installer fallback);
3. clones or hard-updates this fork's `master` branch under `%LOCALAPPDATA%\ChatGPT-Web2API-OpenCode`;
4. creates an isolated virtual environment;
5. installs/updates ChatGPT-Web2API and the OpenCode sidecar;
6. generates a strong local service key when one does not already exist;
7. stores the key in `~/.chatgpt-web2api/opencode-api-key`;
8. merges the OpenCode provider config instead of replacing unrelated settings;
9. references the key through OpenCode's `{file:...}` syntax;
10. configures the tools provider at `http://127.0.0.1:8010/v1` and the direct diagnostic provider at `http://127.0.0.1:8080/v1`;
11. generates reusable launchers and starts the local stack.

**No OpenAI API key is required.** The generated key is only a local password between OpenCode, the sidecar, and Web2API. You do not need to paste that key or the base URL into OpenCode manually.

The only account-interactive step is signing in to ChatGPT in the dedicated Chrome profile opened by Web2API. Never give the installer your ChatGPT password, MFA code, cookies, or session token.

### Optional installer values

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\setup-opencode.ps1 `
  -Upstream "http://127.0.0.1:8080" `
  -BridgeUrl "http://127.0.0.1:8010/v1" `
  -Model "auto"
```

For a normal local install, omit `-ApiKey`; setup generates/reuses the local key automatically. For an existing secured remote Web2API instance, `-ApiKey` must be a key already accepted by that remote server.

Running the installer again is the supported update path. It fetches `master`, resets the managed checkout to that branch, stops only Web2API processes launched from its own managed virtual environment, reinstalls the updated package, re-merges config, and restarts the stack.

## macOS / Linux

Install Git and Python 3.11+ using the OS/package manager first, then:

```bash
git clone https://github.com/ybrizitskiy-hue/ChatGPT-Web2API.git
cd ChatGPT-Web2API
chmod +x scripts/setup-opencode.sh
./scripts/setup-opencode.sh
```

The Windows bootstrap currently has the fullest automatic prerequisite installation path.

## Files created/configured

Typical local files:

```text
~/.chatgpt-web2api/config.json
~/.chatgpt-web2api/opencode-api-key
~/.config/opencode/opencode.json
~/.chatgpt-web2api/start-opencode-web2api.cmd
~/.chatgpt-web2api/start-opencode-web2api.sh
```

The managed Windows source/venv live under:

```text
%LOCALAPPDATA%\ChatGPT-Web2API-OpenCode\source
%LOCALAPPDATA%\ChatGPT-Web2API-OpenCode\venv
```

Existing OpenCode configuration is merged rather than replaced. Timestamped backups are created before rewriting an existing config.

The main provider is equivalent to:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "chatgpt-web": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "ChatGPT Web2API",
      "options": {
        "baseURL": "http://127.0.0.1:8010/v1",
        "apiKey": "{file:C:/Users/YOU/.chatgpt-web2api/opencode-api-key}",
        "timeout": false,
        "headerTimeout": 960000,
        "chunkTimeout": 120000
      },
      "models": {
        "auto": {
          "name": "ChatGPT Web (active browser model)",
          "tool_call": true,
          "reasoning": false
        }
      }
    }
  },
  "model": "chatgpt-web/auto"
}
```

The literal secret is not embedded in OpenCode config.

## Start later

Windows generated launcher:

```text
%USERPROFILE%\.chatgpt-web2api\start-opencode-web2api.cmd
```

Or from the managed/activated Python environment:

```bash
chatgpt-web2api-opencode start
```

Keep the service terminal and the dedicated Chrome process open while using OpenCode.

## Diagnostics

From a source checkout:

```text
scripts\run-opencode-diagnostics.cmd
```

Or run:

```bash
chatgpt-web2api-opencode doctor
```

Healthy output should pass:

```text
Web2API config
API key file
OpenCode provider
Web2API health
Web2API authentication
Bridge health
Model catalog
```

The diagnostic command does not print the local API key.

## Model selection

`auto` is the safest initial model because it does not guess a web model slug. Once logged in, `doctor` shows the live model catalog. You can also query it directly using the generated key file:

```powershell
$key = (Get-Content "$HOME\.chatgpt-web2api\opencode-api-key" -Raw).Trim()
Invoke-RestMethod `
  -Headers @{ Authorization = "Bearer $key" } `
  -Uri "http://127.0.0.1:8080/v1/models"
```

To pin an exact live model, rerun setup with the exact returned ID:

```bash
chatgpt-web2api-opencode setup --model EXACT_MODEL_ID --set-default --non-interactive
```

On the live Windows acceptance machine, `gpt-5.6-sol-wm` was present in the model catalog on 2026-08-19. Treat model IDs as account/UI data, not permanent constants; verify the current catalog before pinning one.

## Tool behavior

OpenCode supplies function schemas to the sidecar. The sidecar gives ChatGPT a strict request envelope. A model request such as:

```json
{"__W2A_TOOL_CALL__":true,"name":"read","arguments":{"filePath":"README.md"}}
```

is translated into a normal OpenAI-compatible `tool_calls` response. The sidecar verifies that the requested name was actually offered and that arguments decode to a JSON object. It never executes local tools itself.

One model-requested tool is emitted per model turn. OpenCode can still perform many sequential turns in one task; live `bash -> read` and create/edit/read/delete loops have been verified.

### False "I cannot access the filesystem" replies

Because ChatGPT Web does not natively know that OpenCode will execute local tools, a model can occasionally answer with a false capability denial even while `read`/`write`/`edit`/`bash` are available. The sidecar contains a bounded recovery specifically for this case:

- only when local-action wording is present;
- only when tools were actually supplied;
- never when `tool_choice=none`;
- one corrective retry only;
- the retry uses `tool_choice=required` so the model must choose one of the offered functions.

Ordinary conversational answers remain `tool_choice=auto` and are not forced through a tool.

## Acceptance smoke tests

Use a disposable project/folder.

### 1. Normal chat

```text
Hi
```

A model answer must return to OpenCode, not merely appear in the managed ChatGPT window.

### 2. Safe shell tool

```text
Use the bash tool. Run exactly: echo OPENCODE_TOOL_OK
You MUST execute the tool; do not answer from memory.
After the tool completes, return only its stdout.
```

Expected final output:

```text
OPENCODE_TOOL_OK
```

### 3. Sequential tool loop

```text
Use the bash tool to create a file named opencode_tool_test.txt containing exactly:

tool-loop-ok

Then use the read tool to read opencode_tool_test.txt back.
Return only the exact contents of the file.
Do not modify any other file.
```

Expected final output:

```text
tool-loop-ok
```

Delete the disposable file afterward if it was not already cleaned up by a later test.

### 4. Write/edit/read/cleanup

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

Expected final output:

```text
TOOLS_FULLY_WORKING
```

## Direct diagnostic baseline

If `chatgpt-web` misbehaves, start a new OpenCode session and choose:

```text
chatgpt-web-direct/auto
```

This bypasses the 8010 tools sidecar and talks to stock Web2API on 8080. If direct chat works but `chatgpt-web` fails, investigate the tools adapter. If direct chat also fails, investigate Web2API/browser response handling instead. Do not modify Chrome/CDP behavior merely to compensate for a sidecar bug.

## Disconnect/retry behavior

The sidecar keeps a browser-backed model turn independent from a single downstream HTTP connection:

- cancelling/disconnecting one OpenCode request does not directly cancel the shared upstream task;
- an identical retry while that logical turn is running can join the same in-memory task;
- successful results can be replayed briefly from an in-memory bounded cache;
- transient 401/429/5xx responses are not retained as successful replay entries;
- SSE keepalives keep OpenCode's downstream connection alive while the browser turn is buffered.

This is **process-local**, not crash-safe exactly-once execution. If the bridge process or machine dies after ChatGPT accepted a write/shell-capable request, inspect the ChatGPT conversation and working tree before repeating that request.

## Known limitations

- Browser automation depends on ChatGPT Web UI/backend behavior.
- Upstream browser turns are buffered; this is not token-by-token browser streaming.
- One tool call per model turn; sequential multi-tool loops work.
- Replay/in-flight state is memory-only and is lost on process/host restart.
- Current OpenCode/title/tool turns can create multiple visible ChatGPT conversations in the managed account. OpenCode sends auxiliary requests (for example title generation), and the bridge currently favors stateless full-history turns over durable browser conversation reuse. Sidebar chat clutter is therefore expected.
- Very long tool sessions resend substantial history/tool schema and may eventually hit model/context limits.
- Keep the local services bound to loopback unless you independently secure remote exposure with TLS, authentication, firewalling, and a security review.

## Troubleshooting

### OpenCode does not show `chatgpt-web`

Run `chatgpt-web2api-opencode doctor`, then fully restart OpenCode so it reloads `~/.config/opencode/opencode.json`.

### OpenCode shows `Thinking` while ChatGPT already answered

Check the service console. If `chatgpt-web-direct` also hangs, the problem is in the stock Web2API response/turn reconciliation path rather than the tools sidecar. If direct works, keep the browser core untouched and inspect the sidecar/tool protocol.

### `README.md is not available in the current workspace`

OpenCode tools operate in the currently opened project directory. Confirm the requested file actually exists there. For a workspace-independent smoke test, use `echo OPENCODE_TOOL_OK` instead.

### HTTP 401

Restart the generated launcher so Web2API reloads `~/.chatgpt-web2api/config.json`. The key in `~/.chatgpt-web2api/opencode-api-key` must match a configured Web2API key.

### Login required

Sign in only in the dedicated Chrome profile opened by Web2API. The profile is reused on later launches.

## Technical details / agent handover

- Bridge design and reliability: [`docs/opencode-bridge.md`](docs/opencode-bridge.md)
- Full handover for Codex/another coding agent: [`docs/opencode-handover.md`](docs/opencode-handover.md)
- Quick bundle instructions: [`scripts/START_HERE.txt`](scripts/START_HERE.txt)
