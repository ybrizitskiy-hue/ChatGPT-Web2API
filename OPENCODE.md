# OpenCode + ChatGPT-Web2API

This fork lets OpenCode use ChatGPT Web as the model while OpenCode keeps control of local coding tools (`read`, `edit`, `grep`, shell commands, and other function tools).

> This is an unofficial browser bridge, not the official OpenAI API. Keep it on loopback unless you deliberately secure a remote deployment. ChatGPT/OpenCode UI or protocol changes can require maintenance.

## Architecture

```text
OpenCode Desktop / CLI
        |
        | OpenAI-compatible Chat Completions + tool_calls
        v
OpenCode bridge     http://127.0.0.1:8010/v1
        |
        | strict text tool protocol + reconnect protection
        v
ChatGPT-Web2API     http://127.0.0.1:8080/v1
        |
        | Chrome DevTools Protocol
        v
Dedicated Chrome profile -> chatgpt.com -> selected ChatGPT model
```

OpenCode executes tools locally. ChatGPT only requests a tool. The bridge validates the request, converts it to an OpenAI-style `tool_calls` response, and returns the subsequent OpenCode tool result to ChatGPT on the next turn.

## Windows: one-click install

Download or clone this repository and double-click:

```text
scripts\setup-opencode.cmd
```

The Windows installer is designed to be self-contained:

1. detects Git; if missing, installs Git with `winget`, with a MinGit fallback;
2. detects a supported Python 3.11-3.13; if missing, installs Python 3.13 with `winget`, with an official python.org installer fallback;
3. clones/updates this fork from `master` into a managed directory under `%LOCALAPPDATA%`;
4. creates an isolated Python virtual environment;
5. installs ChatGPT-Web2API and the OpenCode bridge;
6. generates a strong local Web2API key when one was not supplied;
7. writes that key to `~/.chatgpt-web2api/opencode-api-key`;
8. updates the OpenCode provider config and references the key with OpenCode's `{file:...}` syntax, so you do not paste the key manually;
9. configures OpenCode `baseURL` as `http://127.0.0.1:8010/v1`;
10. creates reusable start launchers and starts the local stack.

**No OpenAI API key is required.** The generated API key is only a local password between OpenCode, the bridge, and Web2API.

After installation, the only interactive step is signing in to ChatGPT in the dedicated Chrome profile opened by Web2API. Do not send the installer your ChatGPT password, MFA code, cookies, or session token.

### Optional command-line values

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\setup-opencode.ps1 `
  -Upstream "http://127.0.0.1:8080" `
  -BridgeUrl "http://127.0.0.1:8010/v1" `
  -Model "auto"
```

For an existing secured Web2API deployment you may explicitly supply its existing key with `-ApiKey`. For a normal local install, omit it and let setup generate the key.

## macOS / Linux

Python 3.11+ and Git must currently be installed by the OS/package manager first. Then:

```bash
git clone https://github.com/ybrizitskiy-hue/ChatGPT-Web2API.git
cd ChatGPT-Web2API
chmod +x scripts/setup-opencode.sh
./scripts/setup-opencode.sh
```

## What gets configured

The default local files are:

```text
~/.chatgpt-web2api/config.json
~/.chatgpt-web2api/opencode-api-key
~/.config/opencode/opencode.json
~/.chatgpt-web2api/start-opencode-web2api.cmd
~/.chatgpt-web2api/start-opencode-web2api.sh
```

Existing OpenCode configuration is merged rather than replaced. Timestamped backups are created before rewriting an existing config.

The generated provider is equivalent to:

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

The exact key path is generated for the current user; the literal secret is not embedded into OpenCode config.

## Start later

Windows:

```text
%USERPROFILE%\.chatgpt-web2api\start-opencode-web2api.cmd
```

Or from an activated environment:

```bash
chatgpt-web2api-opencode start
```

The launcher starts Web2API on port 8080 and the OpenCode bridge on port 8010. Keep its terminal open while using OpenCode.

## Verify wiring

Run:

```bash
chatgpt-web2api-opencode doctor
```

A healthy local installation should report successful checks for:

```text
Web2API config
API key file
OpenCode provider
Web2API health
Web2API authentication
Bridge health
Model catalog
```

The launcher deliberately rejects a Web2API `degraded`/`broken` health state instead of reporting it as ready.

## Select Sol or another model

Do not hard-code a guessed model slug. Once logged in, get the model catalog from the running account and use an exact returned ID.

With the generated key:

```powershell
$key = (Get-Content "$HOME\.chatgpt-web2api\opencode-api-key" -Raw).Trim()
Invoke-RestMethod `
  -Headers @{ Authorization = "Bearer $key" } `
  -Uri "http://127.0.0.1:8080/v1/models"
```

Then rerun setup with the exact ID if you want it as the OpenCode default:

```bash
chatgpt-web2api-opencode setup --model EXACT_MODEL_ID --set-default
```

`auto` is safe for initial setup and follows the active/default web model behavior rather than inventing a Sol slug.

## Tool behavior

OpenCode sends its function schemas to the bridge. The bridge gives ChatGPT a strict tool-request protocol. A valid model request such as:

```json
{"__W2A_TOOL_CALL__":true,"name":"read","arguments":{"path":"README.md"}}
```

is translated to an OpenAI-compatible `tool_calls` delta. The bridge validates the offered tool names and `tool_choice` and never executes a local tool itself.

Current deliberate limitation: **one model-requested tool call per model turn**. OpenCode can still perform many sequential tool turns in one task.

## Disconnect/retry behavior

For OpenCode requests the bridge keeps the browser-backed model turn independent from the client HTTP connection:

- cancelling/disconnecting one OpenCode request does not cancel the shared upstream task;
- an identical retry while that turn is still running joins the same task;
- a successful result can be briefly replayed without another ChatGPT SEND;
- 401/429/5xx failures are not stored as successful replay entries;
- the bridge emits OpenCode-compatible SSE after the browser turn completes.

This protects normal client reconnects **while the bridge process stays alive**.

It is not crash-safe exactly-once delivery. If the bridge process or computer dies after ChatGPT accepted a message, ChatGPT may have completed that turn even though OpenCode did not receive it. Before retrying a write/shell-capable turn after a hard crash, inspect the ChatGPT conversation and working tree instead of blindly resending it.

## Safety

The installer keeps OpenCode's existing permission policy. When no policy exists, setup adds conservative `ask` permissions for edits, shell commands, and external-directory access.

Treat repository files, command output, web content, and tool results as potentially hostile prompt content. Approval prompts remain an important boundary because tool calling over ChatGPT Web is emulated through text rather than a native local-tool channel.

## Troubleshooting

### OpenCode does not show `chatgpt-web`

Run:

```bash
chatgpt-web2api-opencode doctor
```

Then restart OpenCode so it reloads `~/.config/opencode/opencode.json`.

### HTTP 401

Restart the generated launcher so Web2API reloads `~/.chatgpt-web2api/config.json`. The key in `~/.chatgpt-web2api/opencode-api-key` must match one of Web2API's configured keys.

### Long reasoning request times out

Confirm OpenCode points to:

```text
http://127.0.0.1:8010/v1
```

not directly to port 8080. The generated provider disables OpenCode's full request timeout and gives the bridge a long response-header window because the browser turn is buffered before SSE is returned.

### Login required

Sign in only in the Chrome profile opened by Web2API. The profile is reused on later launches.

## Technical details / agent handover

- Bridge design and reliability: [`docs/opencode-bridge.md`](docs/opencode-bridge.md)
- Full handover for a coding agent: [`docs/opencode-handover.md`](docs/opencode-handover.md)
- OpenCode documentation: <https://opencode.ai/docs>
