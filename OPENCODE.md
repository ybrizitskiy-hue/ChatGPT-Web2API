# OpenCode + ChatGPT-Web2API

This integration lets OpenCode use a model selected in the ChatGPT web UI while OpenCode keeps control of local coding tools such as file reads, edits, search, and shell commands.

> This is an unofficial browser bridge. It is not the official OpenAI API and it can break when ChatGPT or OpenCode changes. Keep the bridge bound to localhost unless you deliberately secure a remote deployment.

## Architecture

```text
OpenCode Desktop / CLI
        |
        | OpenAI-compatible chat + tool_calls
        v
OpenCode bridge     http://127.0.0.1:8010/v1
        |
        | text protocol + reconnect protection
        v
ChatGPT-Web2API     http://127.0.0.1:8080/v1
        |
        | Chrome DevTools Protocol
        v
Chrome -> chatgpt.com -> selected ChatGPT model
```

OpenCode executes tools locally. The model only requests a tool. The bridge translates the request into OpenAI-compatible `tool_calls`, OpenCode runs the tool, and the next request carries the tool result back to the model.

## Requirements

- Windows, macOS, or Linux
- Python 3.11 or newer
- Google Chrome or Chromium
- OpenCode Desktop or OpenCode CLI
- A ChatGPT account that can use the model you select

## Easiest Windows setup

Clone or download this repository, then double-click:

```text
scripts\setup-opencode.cmd
```

The script creates an isolated Python environment, installs this fork, runs the setup wizard, writes the OpenCode provider configuration, and starts the local stack. A dedicated Chrome window opens; sign in to ChatGPT there.

PowerShell users can supply values directly:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\setup-opencode.ps1 `
  -ApiKey "my-local-key" `
  -Upstream "http://127.0.0.1:8080" `
  -BridgeUrl "http://127.0.0.1:8010/v1" `
  -Model "auto"
```

The API key is a local password between OpenCode, the bridge, and Web2API. For a local setup it may be omitted; the wizard generates a strong key and stores it in a private file.

## Easiest macOS/Linux setup

```bash
chmod +x scripts/setup-opencode.sh
./scripts/setup-opencode.sh
```

Optional values can be supplied with environment variables:

```bash
W2A_API_KEY='my-local-key' \
W2A_UPSTREAM='http://127.0.0.1:8080' \
W2A_BRIDGE_URL='http://127.0.0.1:8010/v1' \
W2A_MODEL='auto' \
./scripts/setup-opencode.sh
```

## Manual install

From a checkout of this repository:

```bash
python -m venv .venv
```

Activate the environment, then install:

```bash
python -m pip install -e .
```

Run the setup wizard:

```bash
chatgpt-web2api-opencode setup --set-default --start
```

Non-interactive example:

```bash
chatgpt-web2api-opencode setup \
  --non-interactive \
  --api-key 'replace-with-your-key' \
  --upstream http://127.0.0.1:8080 \
  --bridge-url http://127.0.0.1:8010/v1 \
  --model auto \
  --set-default
```

Then start the generated launcher:

- Windows: `~/.chatgpt-web2api/start-opencode-web2api.cmd`
- macOS/Linux: `~/.chatgpt-web2api/start-opencode-web2api.sh`

Or start directly:

```bash
chatgpt-web2api-opencode start
```

## What setup changes

The wizard creates or updates these files and makes timestamped backups before replacing existing configuration:

- `~/.chatgpt-web2api/config.json` — local Web2API configuration
- `~/.chatgpt-web2api/opencode-api-key` — API key referenced by OpenCode
- `~/.config/opencode/opencode.json` — global OpenCode configuration
- `~/.chatgpt-web2api/start-opencode-web2api.cmd`
- `~/.chatgpt-web2api/start-opencode-web2api.sh`

The generated OpenCode provider is equivalent to:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "chatgpt-web": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "ChatGPT Web2API",
      "options": {
        "baseURL": "http://127.0.0.1:8010/v1",
        "apiKey": "{file:/absolute/path/to/.chatgpt-web2api/opencode-api-key}",
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
  "model": "chatgpt-web/auto",
  "permission": {
    "edit": "ask",
    "bash": "ask",
    "external_directory": "ask"
  }
}
```

The wizard preserves unrelated OpenCode settings and existing models under the same provider. It adds the conservative permission policy only when the config has no permission policy yet. Use `--no-safe-permissions` to skip that addition.

## Selecting Sol or another model

The safest model identifier is the exact ID returned by your running account:

```bash
curl -H "Authorization: Bearer YOUR_KEY" \
  http://127.0.0.1:8080/v1/models
```

Then rerun setup with the returned ID:

```bash
chatgpt-web2api-opencode setup \
  --api-key 'YOUR_KEY' \
  --model 'MODEL_ID_FROM_THE_CATALOG' \
  --set-default
```

When `auto` is configured, Web2API does not force a model slug; it uses the model active in the ChatGPT browser UI. This is useful when the web UI exposes a model that is not yet stable enough to hard-code.

After changing the model in ChatGPT, start a new OpenCode session so the model context is clean.

## Project-local OpenCode config

To configure only one project instead of the global OpenCode config:

```bash
cd /path/to/project
chatgpt-web2api-opencode setup \
  --scope project \
  --project-dir . \
  --set-default
```

The wizard writes `opencode.json` in the selected project unless an existing supported project config is found.

## Remote Web2API or remote bridge

A remote Web2API endpoint must already be running and must accept the API key you provide. The wizard does not modify a remote server.

```bash
chatgpt-web2api-opencode setup \
  --api-key 'REMOTE_SERVER_KEY' \
  --upstream 'https://web2api.example.com' \
  --bridge-url 'http://127.0.0.1:8010/v1' \
  --model auto \
  --set-default
```

This starts only the local bridge. When `--bridge-url` is remote too, the generated launcher starts neither service and only verifies that both remote endpoints are reachable.

Do not expose Web2API or the bridge to the public internet without TLS, authentication, firewall restrictions, and a threat model. The bridge forwards the bearer key to Web2API.

## Verify the installation

Run the wiring doctor:

```bash
chatgpt-web2api-opencode doctor
```

A healthy result includes:

```text
OK    Web2API config
OK    API key file
OK    OpenCode provider
OK    Web2API health
OK    Web2API authentication
OK    Bridge health
OK    Model catalog
```

The doctor validates configuration, authentication, and model discovery. It does not spend a ChatGPT turn.

Then test the agent in OpenCode with a harmless task:

```text
Read README.md and report the first heading. Do not edit anything.
```

After that, test permission-gated tool use in a temporary project:

```text
Create a file named opencode-bridge-smoke.txt containing the word OK, read it back, then delete it.
```

OpenCode should ask before editing or running shell commands under the default safety policy.

## Disconnect and recovery behavior

The bridge deliberately buffers the browser-backed model turn before sending a short SSE result to OpenCode. This avoids cancelling the browser turn merely because the client HTTP stream disappears.

| Failure | What happens |
|---|---|
| OpenCode window closes or its request is cancelled while the bridge stays alive | The upstream ChatGPT turn continues. An identical retry joins the in-flight task. |
| Connection returns after the turn completed, while the bridge process is still alive | The successful result can be replayed from memory for the configured TTL, 60 seconds by default. |
| OpenCode retries while the original turn is still running | The retry joins the same in-flight task; the bridge does not send a second ChatGPT message. |
| Bridge process crashes or the computer reboots during generation | The in-memory recovery state is lost. There is no automatic reattachment to that ChatGPT turn. Inspect the browser conversation before retrying because a blind retry may duplicate the message. |
| Web2API or Chrome disconnects | Web2API's own detector/reconciliation handles recoverable browser conditions. The bridge returns a structured 5xx error when the upstream cannot be recovered. |

The bridge also recognizes `Idempotency-Key` and `X-Idempotency-Key`. OpenCode does not currently need to send one for ordinary reconnect protection, but other compatible clients may use these headers.

The replay cache is intentionally short. It protects reconnects, not semantic caching. Change it only with a clear reason:

```bash
chatgpt-web2api-opencode serve --cache-ttl 120
```

## Current limitations

- One model-requested tool call per model turn. OpenCode can perform many tools across successive turns.
- The ChatGPT web response is buffered. OpenCode does not receive true token-by-token browser streaming.
- Recovery state is process-local, not durable across a bridge crash or reboot.
- Browser and web UI changes can break the underlying Web2API selectors or protocol.
- Text tool results are supported. This bridge is not a general multimodal transport.
- The setup does not prove that every ChatGPT model will reliably obey the JSON tool envelope; use the smoke test before real work.

## Troubleshooting

### OpenCode shows no model

Run:

```bash
chatgpt-web2api-opencode doctor
```

Then inspect `~/.config/opencode/opencode.json` and verify that the provider ID and model ID match `chatgpt-web/<model>`.

### HTTP 401

The key in `~/.chatgpt-web2api/opencode-api-key` is not accepted by the running Web2API process. Restart the local launcher so Web2API reloads `~/.chatgpt-web2api/config.json`, or provide the correct remote server key.

### Long reasoning request times out

Ensure OpenCode points to the bridge, not directly to port 8080. The generated provider disables the full request timeout and raises the response-header timeout because the bridge buffers a long browser turn before returning SSE.

### OpenCode repeats a tool call after a hard crash

Stop and inspect the ChatGPT browser conversation and the working tree. Crash-safe turn reconciliation is not implemented. Do not blindly retry write operations.

### The bridge is running but ChatGPT is not logged in

Bring the dedicated Chrome window to the foreground and sign in at `chatgpt.com`. Keep that profile; Web2API reuses it on later launches.

### Port conflict

Choose different local URLs during setup:

```bash
chatgpt-web2api-opencode setup \
  --upstream http://127.0.0.1:8180 \
  --bridge-url http://127.0.0.1:8110/v1 \
  --set-default
```

## Updating

From a repository checkout:

```bash
git pull
python -m pip install -e .
chatgpt-web2api-opencode setup --set-default
chatgpt-web2api-opencode doctor
```

The setup wizard backs up existing configuration before rewriting it.

## Uninstalling

Stop the launcher, remove the `chatgpt-web` provider from the OpenCode config, and optionally remove:

```text
~/.chatgpt-web2api/opencode-api-key
~/.chatgpt-web2api/start-opencode-web2api.cmd
~/.chatgpt-web2api/start-opencode-web2api.sh
```

Keep the Chrome profile if you intend to continue using ChatGPT-Web2API.

## More detail

- Technical design and protocol: [`docs/opencode-bridge.md`](docs/opencode-bridge.md)
- Agent handover and acceptance checklist: [`docs/opencode-handover.md`](docs/opencode-handover.md)
- OpenCode documentation: <https://opencode.ai/docs>
