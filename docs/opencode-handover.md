# Handover: OpenCode + ChatGPT-Web2API

Use this handover for a coding agent working on the target computer. Complete the setup and report concrete pass/fail results. Do not expose authentication material in logs, chat, commits, or reports.

## Target layout

```text
Repository: https://github.com/ybrizitskiy-hue/ChatGPT-Web2API.git
Branch: master
Web2API: http://127.0.0.1:8080
OpenCode bridge: http://127.0.0.1:8010/v1
OpenCode provider: chatgpt-web
```

OpenCode executes local tools. ChatGPT Web only requests a tool through the bridge.

## Safety constraints

- Keep both services on loopback for a normal local setup.
- Preserve existing OpenCode configuration; setup creates timestamped backups.
- Do not collect or display ChatGPT passwords, MFA codes, cookies, session tokens, or the generated local service key.
- Keep existing OpenCode permission policy. When no policy exists, accept the setup default that asks before edit, shell, and external-directory access.
- Use disposable files/directories for write tests.
- After a bridge/host crash, never blindly repeat a write-capable model turn; inspect the ChatGPT conversation and working tree first.

## Windows installation

Preferred path:

```text
scripts\setup-opencode.cmd
```

The installer should perform the following automatically:

1. detect Git;
2. install Git when absent (`winget`, then MinGit fallback);
3. detect Python 3.11-3.13;
4. install Python 3.13 when a supported Python is absent (`winget`, then official python.org installer fallback);
5. clone/update `master` into its managed directory under `%LOCALAPPDATA%`;
6. create an isolated virtual environment and install the package;
7. generate a local Web2API key when none was supplied;
8. store the key in the private Web2API state directory and configure OpenCode to reference it with `{file:...}`;
9. configure OpenCode `baseURL` as `http://127.0.0.1:8010/v1`;
10. create reusable start launchers and start the local stack.

No OpenAI API key is required for this local bridge.

The only account-interactive step should be the human signing in to ChatGPT in the dedicated Chrome profile opened by ChatGPT-Web2API.

## macOS/Linux installation

Git and Python 3.11+ are still OS prerequisites for the shell bootstrap:

```bash
git clone https://github.com/ybrizitskiy-hue/ChatGPT-Web2API.git
cd ChatGPT-Web2API
chmod +x scripts/setup-opencode.sh
./scripts/setup-opencode.sh
```

## Configuration audit

Confirm these files exist after local setup:

```text
~/.chatgpt-web2api/config.json
~/.chatgpt-web2api/opencode-api-key
~/.config/opencode/opencode.json
~/.chatgpt-web2api/start-opencode-web2api.cmd
~/.chatgpt-web2api/start-opencode-web2api.sh
```

Confirm OpenCode has a `chatgpt-web` provider with:

```text
npm = @ai-sdk/openai-compatible
baseURL = http://127.0.0.1:8010/v1
apiKey = {file:...opencode-api-key}
```

Do not print the referenced key. Confirm unrelated OpenCode settings/providers remain and a backup exists when an existing config was changed.

## Start and diagnose

Use the generated launcher or:

```bash
chatgpt-web2api-opencode start
```

Then run:

```bash
chatgpt-web2api-opencode doctor
```

Expected checks:

```text
OK    Web2API config
OK    API key file
OK    OpenCode provider
OK    Web2API health
OK    Web2API authentication
OK    Bridge health
OK    Model catalog
```

A Web2API health state of `degraded` or `broken` is not acceptable as ready.

## Model selection

Start with `auto` unless an exact model identifier has been verified from the live `/v1/models` catalog. Never invent a Sol/reasoning slug.

If the desired Sol model is present in the catalog, rerun setup with that exact ID and `--set-default`. Otherwise leave `auto` and report that the exact model slug was not proven.

## OpenCode acceptance test

Use a disposable project.

First run a read-only task:

```text
Read README.md and report its first heading. Do not modify any files.
```

Pass criteria:

- OpenCode sends tools to the bridge;
- ChatGPT requests one offered tool;
- the bridge emits an indexed OpenAI-compatible `tool_calls` delta;
- OpenCode executes the tool locally;
- the tool result reaches ChatGPT on the next turn;
- ChatGPT produces a final answer.

Then, if the user approves a write test, use a temporary file and clean it up afterward. OpenCode should ask for the relevant permission under the default safety policy.

## Reconnect acceptance test

Use a non-destructive request. Disconnect/cancel the OpenCode client request while keeping both local services alive, then retry the identical logical request.

Pass criteria:

- client cancellation does not cancel the shared upstream model task;
- the identical retry joins the in-flight task or receives the short replayed success;
- a second ChatGPT SEND is not caused solely by the client reconnect.

This guarantee is process-local. A bridge crash or host reboot is not exactly-once recoverable.

## Development validation

For source changes, run:

```bash
python -m compileall -q src tests
ruff check src tests
pytest -v -m "not e2e"
```

On Windows, also parse and dry-run the bootstrap:

```powershell
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File scripts/setup-opencode.ps1 -DryRun
```

The live ChatGPT-account smoke test is separate from CI because it uses the real account/browser session.

## Final report

Return:

```text
RESULT: PASS / PARTIAL / FAIL
OS:
Python:
Git:
OpenCode:
Repository commit:
Web2API health: PASS/FAIL
Bridge health: PASS/FAIL
OpenCode provider: PASS/FAIL
Selected model: <verified id or auto>
Read tool loop: PASS/FAIL/NOT RUN
Write tool loop: PASS/FAIL/NOT RUN
Reconnect test: PASS/FAIL/NOT RUN
Notes:
```

Never include secret values in the report.
