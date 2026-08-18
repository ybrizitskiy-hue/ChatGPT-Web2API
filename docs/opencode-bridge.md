# OpenCode bridge

This branch adds a thin compatibility layer for using ChatGPT-Web2API as an
OpenCode coding-model provider.

## Why a bridge is needed

The core Web2API endpoint accepts OpenAI-style messages but intentionally sends
plain text through ChatGPT Web. OpenCode, however, expects structured
`tool_calls` for local tools such as file reads, edits, grep, and shell commands.

`chatgpt_web2api.opencode_bridge` translates between those two protocols and
adds short-lived request replay/coalescing so a reconnect does not immediately
submit the same logical turn to ChatGPT twice.

## Run

Start ChatGPT-Web2API normally on port 8000, then start the bridge:

```bash
python -m chatgpt_web2api.opencode_bridge
```

Defaults:

- bridge: `http://127.0.0.1:8010`
- upstream Web2API: `http://127.0.0.1:8000`
- replay cache TTL: 300 seconds
- upstream request timeout: 930 seconds

Environment overrides:

```bash
export W2A_UPSTREAM=http://127.0.0.1:8000
export W2A_OPENCODE_PORT=8010
export W2A_OPENCODE_CACHE_TTL=300
export W2A_OPENCODE_TIMEOUT=930
```

## OpenCode config

Use OpenCode's OpenAI-compatible provider against the bridge, not directly
against port 8000:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "chatgpt-web": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "ChatGPT Web2API",
      "options": {
        "baseURL": "http://127.0.0.1:8010/v1",
        "apiKey": "local"
      },
      "models": {
        "gpt-5-5-thinking": {
          "name": "ChatGPT Web reasoning"
        }
      }
    }
  }
}
```

Replace the model id with one returned by the upstream `GET /v1/models` on
your account.

## Disconnect / retry behaviour

For tool-enabled requests the bridge asks Web2API for a non-streaming upstream
completion, even when OpenCode requested streaming. It then returns a single
OpenAI SSE chunk to OpenCode. This is deliberate:

1. the ChatGPT turn can finish independently of the client HTTP stream;
2. disconnect/cancellation of one OpenCode request does not cancel the shared
   upstream task (`asyncio.shield`);
3. an identical request that reconnects while the first is still running joins
   the same in-flight task;
4. an identical request within the cache TTL receives the completed result
   without another ChatGPT SEND.

The fingerprint ignores only the `stream` transport flag. Model, messages,
tools, tool choice, and other logical request fields remain part of the key.

## Tool-call protocol

OpenCode's tool schemas are injected into the web-model system instructions.
When the model needs a tool it is instructed to emit exactly one JSON object:

```json
{"__W2A_TOOL_CALL__":true,"name":"read","arguments":{"path":"README.md"}}
```

The bridge converts that into a normal OpenAI `tool_calls` response. The next
OpenCode `role=tool` result is converted into visible conversation context for
Web2API.

Current limitation: one tool call per model turn. Parallel tool calls are not
yet supported. This keeps the first implementation deterministic and easier to
recover safely after network interruptions.

## Important reliability boundary

The replay cache is process-local. It protects ordinary client disconnects and
retries while the bridge process remains alive. A bridge process crash or host
reboot loses the cache. Durable idempotency across bridge restarts would require
persisting request fingerprints plus ChatGPT conversation/turn reconciliation,
and should be implemented separately rather than silently retrying SEND.
