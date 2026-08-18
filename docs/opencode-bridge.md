# OpenCode bridge: design and reliability

This integration exposes ChatGPT-Web2API to OpenCode as an OpenAI-compatible coding-model provider with structured tool calls.

For installation and day-to-day use, start with [`../OPENCODE.md`](../OPENCODE.md). For an autonomous coding-agent handover, use [`opencode-handover.md`](opencode-handover.md).

## Why the extra bridge exists

The core Web2API endpoint sends ordinary text through the ChatGPT web interface. OpenCode expects a coding model to return structured `tool_calls` for local operations such as reading files, editing, searching, and invoking a shell.

The OpenCode bridge performs four jobs:

1. projects OpenCode function schemas into strict model instructions;
2. translates the model's JSON tool request into OpenAI `tool_calls` SSE;
3. converts OpenCode `role=tool` messages back into visible model context;
4. coalesces and briefly replays identical logical requests while the bridge process remains alive.

OpenCode, not ChatGPT-Web2API, executes local tools.

## Runtime layout

```text
OpenCode
  -> http://127.0.0.1:8010/v1/chat/completions
  -> chatgpt_web2api.opencode_bridge
  -> http://127.0.0.1:8080/v1/chat/completions
  -> Chrome / ChatGPT web
```

The supported launcher is:

```bash
chatgpt-web2api-opencode serve \
  --upstream http://127.0.0.1:8080 \
  --host 127.0.0.1 \
  --port 8010
```

The setup wizard and generated launchers call this command automatically.

## Tool protocol

The bridge gives the model the available function names, descriptions, and JSON schemas. When a tool is required, the model must emit one object:

```json
{"__W2A_TOOL_CALL__":true,"name":"read","arguments":{"path":"README.md"}}
```

The bridge validates that:

- the selected name was actually offered by OpenCode;
- `tool_choice=none`, `required`, and a named function are respected;
- arguments decode to a JSON object;
- malformed protocol output never becomes a local tool execution.

A valid request becomes a normal OpenAI tool-call delta containing `index`, `id`, `type`, function name, and encoded arguments. A separate event carries `finish_reason: "tool_calls"`, followed by `[DONE]`.

The next OpenCode request contains the assistant tool call and `role=tool` result. The bridge renders those messages as explicit conversation context for ChatGPT Web, allowing the model to request another tool or produce a final response.

## Streaming model

The upstream browser turn is intentionally requested non-streaming, even when OpenCode asks for SSE. After Web2API finishes, the bridge emits a short standards-shaped SSE response.

This buffering is a reliability tradeoff:

- a disappearing OpenCode HTTP client does not directly cancel the browser generation;
- the bridge can share one upstream task between identical reconnects;
- OpenCode still receives the tool-call shape its parser expects;
- token-by-token browser streaming is not available.

## Disconnect and duplicate-send protection

The bridge fingerprints the logical request. Transport-only streaming fields are excluded; model, messages, tools, tool choice, authentication context, and other logical fields remain part of the key.

While the bridge process is alive:

1. the first request creates one upstream task;
2. cancellation of an awaiting client is shielded from that task;
3. an identical retry joins the existing task;
4. a successful result is replayable for the configured TTL, 60 seconds through the audited launcher;
5. transient upstream/auth/rate-limit/server failures are not retained as successful replay entries.

This protects the common case where OpenCode loses its HTTP connection but the local bridge and Web2API continue running.

## Hard reliability boundary

Recovery state is stored in bridge memory. If the bridge process or host dies, there is currently no durable mapping from an OpenCode request to the ChatGPT conversation turn that may already have completed.

Therefore, after a bridge crash or reboot:

- do not assume the request failed;
- inspect the ChatGPT browser conversation;
- inspect the working tree and terminal side effects;
- do not blindly retry a write-capable turn.

True crash-safe reattachment would require a persistent request journal, ChatGPT conversation/turn reconciliation, and explicit idempotency semantics. It should not be approximated by automatically issuing a second SEND.

## Setup layer

`chatgpt_web2api.opencode_setup` provides:

- JSON and JSONC-aware OpenCode config updates;
- timestamped backups;
- a separate key file referenced with OpenCode's `{file:...}` syntax;
- preservation of existing provider options and models;
- optional conservative `ask` permissions for edit, bash, and external directories;
- local or remote Web2API/bridge URLs;
- generated `.cmd` and `.sh` launchers;
- authentication and model-catalog diagnostics through `doctor`.

The setup layer never modifies a remote Web2API server. For a remote endpoint, the user must provide a key already accepted by that server.

## Current limitations

- One model-requested tool call per model turn; multiple tools work across successive turns.
- No parallel tool-call envelope.
- No durable recovery across bridge restart or host reboot.
- Buffered rather than token-by-token streaming.
- Browser automation remains sensitive to ChatGPT web changes.
- The bridge is intended for loopback use by default; remote deployment requires TLS, authentication, firewalling, and independent security review.

## Test coverage

The branch includes tests for:

- tool schema injection and message normalization;
- tool-choice validation and malformed calls;
- OpenAI tool-call translation and SSE framing;
- HTTP round-trip through a fake Web2API upstream;
- in-flight coalescing and replay behavior;
- JSONC parsing, backups, provider merging, key-file references, safety defaults, and launchers;
- Linux/macOS/Windows package tests through GitHub Actions.

A real-account end-to-end test still requires the operator to sign in to the dedicated ChatGPT Chrome profile and run the acceptance steps in [`opencode-handover.md`](opencode-handover.md).
