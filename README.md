# miniagent

A simplified personal AI gateway with browser control — built to understand how production AI agent systems actually work from the inside.

```
Browser UI  ──WS──►  Gateway :18789  ──asyncio.Task──►  Claude API
                          │                                   │
                          │◄──── streaming tokens ────────────┘
                          │
                          └──HTTP──►  Browser Server :18790  ──►  Playwright Chromium
```

## Why I built this

I wanted to deeply understand how a production AI agent system is actually structured — not from tutorials, but by building a real working system from scratch.

The reference I used was [OpenClaw](https://github.com/openagent-dev/openclaw), a production-grade AI gateway. OpenClaw is well-architected but large (TypeScript, multi-provider, multi-channel). I built miniagent as a Python translation of its core ideas, stripped down to the minimum needed to understand the patterns:

- One AI provider (Anthropic Claude)
- One channel (browser WebSocket)
- One browser backend (Playwright Chromium)

The goal wasn't to build something novel — it was to **understand why each piece exists** by having to build it myself. You can't really understand a broadcaster, a connection lifecycle, or a double-response RPC pattern by reading about them. You have to wire them up and watch them break.

## What I learned

### The gateway isn't just a proxy

A naive implementation would forward messages directly from the WebSocket to the API. The real architecture is more careful:

- The WebSocket **connection lifecycle** is a state machine (pending → connected → closed), with a handshake timeout and a `connect.challenge` to confirm the client is real before anything is registered
- The **broadcaster** fan-out is separate from the connection — events flow from the agent pipeline through an in-process event emitter, then to the broadcaster, then to every connected client. This means multiple tabs see the same run
- The **double-response pattern** for `agent` requests matters: the handler sends an immediate `accepted` ack, then fires an `asyncio.Task`. The WebSocket is unblocked immediately; the background task sends a second response when the run finishes. Without this, the UI would hang waiting for a response that could take minutes

### Process isolation is load-bearing

The browser server runs on its own port (`:18790`) and is called over HTTP, never imported directly. This looks like over-engineering at first. The reason: if Playwright crashes (and it does, on complex JS-heavy pages), it takes down its own process only. The gateway and active sessions survive. You restart the browser server and continue.

This is the same pattern OpenClaw uses — browser isolation isn't a deployment detail, it's a resilience decision baked into the architecture.

### Screenshots and the context window

Sending a screenshot to Claude as `str({"image_b64": "..."})` produces ~800,000 tokens. The correct approach is Anthropic's vision image block format — the same PNG sent as a properly typed base64 block costs ~2,000–3,000 tokens and Claude can actually see it. This isn't obvious from the docs until you hit the 200k token limit error in production.

Similarly, Playwright's `full_page=True` captures the entire scroll height of a page — news sites can be 15,000px tall. Anthropic's vision API rejects images over 8,000px in either dimension. Viewport-only screenshots stay within limits.

### Session keys and conversation scope

Every message in the same session shares history. That's by design — it's how multi-turn conversations work. The subtlety is that "session" needs to be scoped deliberately: the same hardcoded key across all browser tabs means tab A's Google News search bleeds into tab B's Yahoo News question. Generating a unique key per page load gives each conversation a clean scope while still supporting multi-turn within a session.

### Click interception on JS-heavy sites

Modern sites like Google News use overlapping `<div>` elements with custom `jsaction` handlers that intercept pointer events before they reach the actual link. Playwright's `locator.click()` times out because it correctly detects that another element would intercept the click. The fix is `dispatch_event('click')` as a fallback — this fires the event directly on the target DOM node, bypassing pointer interception entirely.

## How it was built — parallel AI sub-agents

The entire project was built using [Claude Code](https://claude.ai/claude-code) with a parallel sub-agent workflow. Rather than writing code sequentially, multiple agents worked concurrently on independent modules.

The key insight that made this work: **the modules were designed to have clean interfaces before any code was written**. `config/` and `protocol/` have no internal dependencies, so they were built in parallel. `sessions/` and `browser/` only depend on `config/`, so they ran in parallel next. `agent/` depended on `config/` and `protocol/`. `gateway/` wired everything together last.

Each agent received:
1. The exact files it owned
2. The relevant DESIGN.md section
3. A clear interface contract (what it imports, what imports it)
4. Instructions to update a shared `MEMORY.md` when done

The `CLAUDE.md` file at the project root was injected into every agent automatically, so conventions (async everywhere, Pydantic v2, no real API calls in tests) were enforced without repeating them in every prompt.

The whole thing — 197 tests, 6 modules, a working browser agent — was built in a single session.

## Features

- **Streaming chat** — tokens stream in real time over WebSocket, rendered as markdown when complete
- **Browser control** — Claude can navigate, screenshot, click, type, and scroll
- **Session memory** — conversation history persisted as JSONL, scoped per session key
- **Single-file UI** — zero build step, vanilla JS, dark theme, inline markdown renderer
- **Process isolation** — browser server runs independently; gateway survives browser crashes
- **197 tests** — fully mocked with `AsyncMock`, no real API calls needed

## Quick start

**Prerequisites:** Python 3.12+, [uv](https://docs.astral.sh/uv/)

```bash
# 1. Clone and install
git clone https://github.com/ridhamdave/miniagent
cd miniagent
uv sync

# 2. Install Playwright browser
uv run playwright install chromium

# 3. Set your Anthropic API key
cp .env.example .env
# edit .env → set MINIAGENT_ANTHROPIC_API_KEY=sk-ant-...

# 4. Start
uv run python -m miniagent
```

Open **http://localhost:18789** in your browser.

## Configuration

`config.yaml` (committed with safe defaults):

```yaml
gateway:
  port: 18789
  host: "127.0.0.1"

browser:
  control_port: 18790
  headless: false      # set true to hide the browser window
  timeout_ms: 8000

agent:
  model: "claude-sonnet-4-6"
  max_tokens: 8096
```

Environment variables always win (prefix `MINIAGENT_`):

```bash
MINIAGENT_ANTHROPIC_API_KEY=sk-ant-...
MINIAGENT_PORT=18789
MINIAGENT_BROWSER_PORT=18790
```

## Project structure

```
miniagent/
├── config/       Pydantic config — types, env settings, singleton loader
├── protocol/     WebSocket frames — RequestFrame, ResponseFrame, EventFrame, error codes
├── gateway/      FastAPI WebSocket server, connection lifecycle, RPC dispatch
│   └── handlers/ agent (double-response), chat (history/abort), browser (HTTP proxy)
├── agent/        Anthropic streaming pipeline, recursive tool loop, event emitter
├── browser/      Standalone FastAPI HTTP server — Playwright Chromium on port 18790
├── sessions/     JSONL append/load/clear — one file per session key
└── ui/           index.html — single-file vanilla JS chat, streaming cursor, markdown

tests/
├── test_config.py          30 tests — config merging, env override, singleton
├── test_protocol.py        43 tests — Pydantic round-trips, literal discriminators
├── test_sessions.py        15 tests — JSONL append/load/clear, tmp_path isolation
├── test_browser.py         35 tests — route handlers, lifespan, mock Playwright
├── test_agent_pipeline.py  38 tests — mock Anthropic streaming, tool call loop
└── test_gateway.py         36 tests — WebSocket handshake, RPC dispatch, broadcaster
```

## Running tests

```bash
uv run pytest              # all 197 tests (~0.6s)
uv run pytest -v           # verbose with test names
uv run pytest tests/test_agent_pipeline.py  # single module
```

## Architecture decisions

| Decision | Why |
|----------|-----|
| Browser on a separate port | Crash isolation — Playwright can fail without taking down the gateway |
| Double-response for `agent` RPC | Unblocks the WebSocket immediately; background task streams tokens |
| Event emitter → broadcaster → WebSocket | Decouples pipeline from transport; multiple tabs see the same run |
| JSONL for session storage | Append-only, human-readable, no database dependency |
| Screenshots as vision blocks | `str(base64)` = 800k tokens; typed image block = ~2k tokens |
| Per-page-load session key | Each browser tab gets a clean conversation scope |
| `dispatch_event('click')` fallback | Bypasses pointer interception on JS-heavy sites (Google News, etc.) |

## Stack

| Layer | Library |
|-------|---------|
| Web framework | FastAPI + uvicorn |
| WebSocket | Starlette native |
| AI provider | `anthropic` SDK (async streaming) |
| Browser | Playwright (async Chromium) |
| HTTP client | aiohttp |
| Config | Pydantic v2 + pydantic-settings |
| Tests | pytest + pytest-asyncio |
| Package manager | uv |
