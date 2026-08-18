# Handover: install and validate OpenCode + ChatGPT-Web2API

Give this document to a coding agent that has terminal access to the target computer. The agent should complete the work, not merely describe it.

## Goal

Configure OpenCode to use the `chatgpt-web` provider through the local OpenCode bridge, launch the Web2API Chrome profile, let the human sign in to ChatGPT, select the desired account model, and prove a full tool loop in a disposable project.

## Safety constraints

1. Do not paste or commit the API key into a repository.
2. Do not expose ports 8080 or 8010 beyond loopback.
3. Do not overwrite an existing OpenCode config without preserving it. The setup wizard creates timestamped backups; verify they exist when a config was already present.
4. Use a temporary project for write/bash testing.
5. Do not blindly repeat a request after a bridge process crash. Inspect the ChatGPT conversation and working tree first.
6. Do not change an existing OpenCode permission policy unless the user explicitly asks.

## Inputs

Obtain or resolve:

- repository checkout path;
- Python 3.11+ executable;
- OpenCode Desktop or CLI installation;
- Web2API URL, normally `http://127.0.0.1:8080`;
- bridge URL, normally `http://127.0.0.1:8010/v1`;
- API key, or permission to generate one for a local setup;
- desired model. Use `auto` initially when the live model slug is unknown.

For a remote Web2API URL, the API key must already be accepted by that remote server. The setup cannot modify a remote server.

## Procedure

### 1. Inspect prerequisites

Run:

```bash
python --version
```

Require Python 3.11 or newer. Confirm Chrome/Chromium and OpenCode are installed. Do not install unrequested system-wide packages when an isolated virtual environment is sufficient.

### 2. Install the fork in an isolated environment

From the repository root:

```bash
python -m venv .venv
```

Activate the environment and run:

```bash
python -m pip install --upgrade pip
python -m pip install -e .
```

Confirm:

```bash
chatgpt-web2api-opencode --help
chatgpt-web2api-opencode setup --help
```

### 3. Configure the provider

For a local setup with an automatically generated key:

```bash
chatgpt-web2api-opencode setup \
  --non-interactive \
  --upstream http://127.0.0.1:8080 \
  --bridge-url http://127.0.0.1:8010/v1 \
  --model auto \
  --set-default
```

When the user supplied a key, add:

```text
--api-key USER_SUPPLIED_KEY
```

Never include that command in logs or reports containing the literal secret. Redact it.

Expected files:

```text
~/.chatgpt-web2api/config.json
~/.chatgpt-web2api/opencode-api-key
~/.config/opencode/opencode.json
~/.chatgpt-web2api/start-opencode-web2api.cmd
~/.chatgpt-web2api/start-opencode-web2api.sh
```

Check that the key file is referenced as `{file:...}` in the OpenCode config and that the key itself is not embedded there.

### 4. Start the stack

Run the generated launcher or:

```bash
chatgpt-web2api-opencode start
```

Keep the launcher terminal open. A dedicated Chrome profile should open. Ask the human to sign in to ChatGPT in that window. Do not request their password or cookies.

### 5. Validate wiring

Run in a second terminal using the same environment:

```bash
chatgpt-web2api-opencode doctor
```

All checks should pass. If authentication fails and Web2API was already running before setup, restart it so it reloads the updated key configuration.

Also verify directly, redacting the key in any report:

```bash
KEY="$(cat ~/.chatgpt-web2api/opencode-api-key)"
curl -sS -H "Authorization: Bearer $KEY" http://127.0.0.1:8010/v1/models
```

### 6. Select the desired model

Read the IDs returned by `/v1/models`. When an ID corresponding to the user's desired Sol/reasoning model is present, rerun:

```bash
chatgpt-web2api-opencode setup \
  --non-interactive \
  --model 'EXACT_MODEL_ID' \
  --set-default
```

When no stable slug is available, retain `auto` and have the human select the desired model in the ChatGPT web UI. Start a fresh OpenCode session after switching models.

### 7. Test a read-only tool loop

Create or open a disposable project containing a small `README.md`. In OpenCode select `chatgpt-web/<model>` and ask:

```text
Read README.md using the available file tool and report its first heading. Do not edit anything.
```

Acceptance evidence:

- OpenCode receives a structured tool call rather than raw sentinel JSON;
- OpenCode executes the read tool;
- the tool result is returned to the model;
- the final answer matches the file.

### 8. Test permission-gated mutation

In the disposable project ask:

```text
Create opencode-bridge-smoke.txt containing OK, read it back, and then delete it.
```

Acceptance evidence:

- OpenCode asks permission before edit/bash under the generated default policy;
- the requested file lifecycle completes;
- no unrelated files change.

### 9. Test reconnect behavior

Use a harmless long reasoning prompt. While it is running, interrupt only the OpenCode client connection or close/reopen the OpenCode view; do not stop the bridge or Web2API processes. Retry the identical request.

Acceptance evidence:

- Web2API receives one logical ChatGPT send, not two;
- the retry joins the in-flight bridge task or receives its short replay;
- the result appears in OpenCode.

This test does not prove crash recovery. Then stop the bridge during another harmless request and confirm the documented limitation: there is no automatic reattachment after process death. Inspect the ChatGPT browser conversation before any manual retry.

### 10. Final report

Report:

- OS and Python version;
- installation path;
- OpenCode config path;
- Web2API and bridge URLs;
- provider/model ID;
- doctor result;
- read-only tool-loop result;
- mutation test result;
- reconnect test result;
- any warnings or remaining limitations.

Redact the API key. Do not claim crash-safe recovery.

## Troubleshooting decision tree

### `chatgpt-web2api-opencode` not found

Use the environment's Python directly:

```bash
python -m chatgpt_web2api.opencode_bridge --help
```

Reinstall with `python -m pip install -e .`.

### Doctor reports HTTP 401

The running core did not load the configured key or the remote key is wrong. Restart the local launcher, or obtain the correct remote key. Do not disable authentication as a shortcut.

### Doctor reports bridge health failure

Check whether port 8010 is occupied. Start manually with debug logging:

```bash
chatgpt-web2api-opencode --log-level DEBUG serve \
  --upstream http://127.0.0.1:8080 \
  --port 8010
```

### `/v1/models` works but OpenCode lists no model

Inspect `provider.chatgpt-web.models` in the OpenCode config. Confirm the root model is `chatgpt-web/<exact-id>`. Restart OpenCode after editing the config.

### Raw `__W2A_TOOL_CALL__` JSON appears in chat

Confirm OpenCode points to port 8010, not directly to port 8080. Check that the configured model has `tool_call: true`. Run the bridge tests and inspect bridge logs.

### Tool calls time out before the model answers

Confirm provider options include:

```json
{
  "timeout": false,
  "headerTimeout": 960000,
  "chunkTimeout": 120000
}
```

### A write might have run twice

Stop. Inspect the working tree, terminal history, and ChatGPT conversation before continuing. The bridge prevents ordinary reconnect duplicates while alive, but cannot guarantee recovery across bridge process death or reboot.

## Acceptance checklist

- [ ] Python 3.11+ and Chrome are available.
- [ ] The package installs and `chatgpt-web2api-opencode --help` works.
- [ ] Existing configs were backed up before modification.
- [ ] The API key is stored outside the OpenCode config and redacted from reports.
- [ ] Services bind to loopback.
- [ ] The human signed in to the dedicated ChatGPT Chrome profile.
- [ ] `doctor` passes.
- [ ] `/v1/models` returns the catalog through the bridge.
- [ ] OpenCode selects `chatgpt-web/<model>`.
- [ ] Read-only tool loop passes.
- [ ] Permission-gated edit/bash loop passes in a disposable project.
- [ ] Ordinary disconnect/retry produces no duplicate send.
- [ ] Crash/reboot limitation was observed and documented accurately.
