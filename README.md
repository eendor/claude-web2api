# claude-web2api

A small, (almost) zero-dependency proxy that exposes your **claude.ai web** session as an
**OpenAI-compatible API** (`/v1/chat/completions`), so OpenAI-compatible clients — in
particular [opencode](https://opencode.ai) — can use your Claude subscription (Sonnet, etc.)
as a model, **including agentic tool use**.

> The only dependency is [`curl_cffi`](https://github.com/lexiforest/curl_cffi), used to
> impersonate a browser's TLS so Cloudflare lets the requests through. Everything else is the
> Python standard library.

> **Disclaimer** — This is an unofficial tool that drives the consumer claude.ai web app with
> your own logged-in session. It is **not** the Anthropic API and is not affiliated with
> Anthropic. Automating the consumer app may violate Anthropic's Terms of Service. Use your own
> account, at your own risk.

## What it does

- Serves `POST /v1/chat/completions` (streaming + non-streaming) and `GET /v1/models`.
- **No key to paste**: reads your claude.ai cookies straight from your Firefox profile
  (`sessionKey`, `cf_clearance`, …) and refreshes them as they rotate.
- **Agentic tool use**: claude.ai web has no function-calling, so tools are emulated at the
  prompt level — tool schemas are injected and the reply's `<tool_calls>` block is parsed back
  into OpenAI `tool_calls`. Verified with opencode's build agent (read/write/edit/bash).
- **Conversation reuse**: keeps one claude.ai thread per session and sends only each new turn
  instead of replaying the whole history every tool step — much easier on your usage limits.
- **Native web search** + configurable thinking **effort** (`low`…`max`).
- **Usage meter**: `GET /usage` reports how much of your rolling **5h / 7d** limits you've used
  (claude.ai doesn't expose token counts; this is its own metric).

## Requirements

- Python 3.9+
- `curl_cffi` — `pip install curl_cffi` (or `pip install -r requirements.txt`)
- A Firefox profile logged into claude.ai (or paste a session key manually).

## Run

```sh
COOKIE_FROM=firefox python3 claude_web2api.py
# -> serving http://127.0.0.1:8080/v1
```

Offline self-test (no network, no account needed):

```sh
python3 claude_web2api.py --selftest
```

## Use it from opencode

Add a provider to `~/.config/opencode/opencode.json`:

```jsonc
{
  "provider": {
    "claude-web": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "Claude (web session)",
      "options": { "baseURL": "http://127.0.0.1:8080/v1", "apiKey": "local" },
      "models": {
        "sonnet": {
          "name": "Claude Sonnet (web)",
          "tool_call": true,
          "limit": { "context": 200000, "output": 64000 }
        }
      }
    }
  }
}
```

Then select `claude-web/sonnet` via `/models`. `"sonnet"` resolves to the newest Sonnet your
account offers.

### Optional: start/stop the proxy together with opencode

Wrap the `opencode` command in your shell rc so the proxy starts on launch and stops on exit:

```sh
opencode() {
  local pid=""
  if ! curl -sf -o /dev/null http://127.0.0.1:8080/health; then
    COOKIE_FROM=firefox python3 ~/claude-web2api/claude_web2api.py >/tmp/claude-web2api.log 2>&1 &
    pid=$!
    for _ in {1..25}; do curl -sf -o /dev/null http://127.0.0.1:8080/health && break; sleep 0.2; done
  fi
  command opencode "$@"
  [ -n "$pid" ] && kill "$pid" 2>/dev/null
}
```

## Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/v1/chat/completions` | OpenAI chat completions (streaming + tools) |
| `GET`  | `/v1/models` | models available to your account |
| `GET`  | `/usage` | your rolling 5h / 7d usage percentages |
| `GET`  | `/health` | liveness probe |

## Configuration (environment variables)

| Var | Default | Meaning |
|-----|---------|---------|
| `COOKIE_FROM` | – | `firefox` (auto-find profile) or a path to a profile dir / `cookies.sqlite` |
| `CLAUDE_SESSION_KEY` | – | `sessionKey` cookie value (manual alternative to `COOKIE_FROM`) |
| `CLAUDE_COOKIE` | – | full `Cookie:` header (manual; carries `cf_clearance`) |
| `IMPERSONATE` | `firefox` if `COOKIE_FROM` else `chrome` | curl_cffi TLS fingerprint |
| `DEFAULT_MODEL` | `claude-sonnet-4-5-20250929` | fallback slug when none matches |
| `EFFORT` | `low` | thinking effort: `low` / `medium` / `high` / `xhigh` / `max` |
| `WEB_SEARCH` | `true` | enable claude.ai's native web search |
| `API_KEY` | – | if set, require `Authorization: Bearer <key>` |
| `HOST` / `PORT` | `127.0.0.1` / `8080` | listen address |
| `PROXY` | – | upstream proxy URL for the claude.ai requests |
| `DELETE_AFTER` | `true` | delete reused threads on exit / when idle |
| `SESSION_TTL` | `1800` | seconds a reused thread is kept before it's dropped + deleted |
| `MAX_SESSIONS` | `40` | max reused threads kept at once |
| `MAX_PROMPT_CHARS` | `120000` | trim old turns past this (full-send fallback only) |
| `TOOL_RESULT_CAP` | `6000` | per-tool-result character cap |

## How it works

claude.ai's web completion endpoint takes a single `prompt` per turn and has no native
function-calling. This proxy:

1. Reads your browser cookies and impersonates the browser's TLS fingerprint so Cloudflare is
   satisfied.
2. For the first turn of a session it sends the system prompt + tool schemas; after that it
   **reuses the same claude.ai thread** and sends only each new turn (tool results / follow-ups),
   letting Claude hold the context server-side.
3. Injects tool schemas + instructions so Claude emits a `<tool_calls>` JSON block, then parses
   that back into OpenAI `tool_calls`.

## Limitations

- Prompt-level tool-calling is less reliable than a native function-calling API. It works well
  with Sonnet but can occasionally need a retry.
- No token counts (claude.ai doesn't send any) — usage is the 5h / 7d percentages only.
- Firefox only for automatic cookie reading (its cookies are unencrypted on disk on Linux).
- Unofficial; see the disclaimer above.
