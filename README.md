# miniagent

A simplified personal AI gateway that mirrors [OpenClaw](https://github.com/openagent-dev/openclaw)'s core architecture — single provider (Anthropic Claude), single channel (browser Web UI), and full browser control via Playwright.

Built as a learning project to understand how production AI gateway systems work from the inside.

```
Browser UI  ──WS──►  Gateway :18789  ──asyncio.Task──►  Claude API
                          │                                   │
                          │◄──── streaming tokens ────────────┘
                          │
                          └──HTTP──►  Browser Server :18790  ──►  Playwright Chromium
```

## Features

- **Streaming chat** — tokens stream in real time over WebSocket
- **Browser control** — Claude can navigate, screenshot, click, type, scroll
- **Session memory** — conversation history persisted as JSONL per session
- **Single-file UI** — zero build step, vanilla JS, dark theme, markdown rendering
- **197 tests** — fully mocked, no real API calls needed to run the suite

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
# edit .env and set MINIAGENT_ANTHROPIC_API_KEY=sk-ant-...

# 4. Start
uv run python -m miniagent
```

Open **http://localhost:18789** in your browser.

## Configuration

Edit `config.yaml` to change ports, model, or browser settings:

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

Environment variables override `config.yaml` (prefix: `MINIAGENT_`):

```bash
MINIAGENT_ANTHROPIC_API_KEY=sk-ant-...
MINIAGENT_PORT=18789
MINIAGENT_BROWSER_PORT=18790
```

## Project structure

```
miniagent/
├── config/       Pydantic config — types, env settings, singleton loader
├── protocol/     WebSocket frame types — RequestFrame, ResponseFrame, EventFrame
├── gateway/      FastAPI WebSocket server, RPC handlers, broadcaster
│   └── handlers/ agent, chat, browser RPC handlers
├── agent/        Anthropic streaming pipeline, tool loop, event emitter
├── browser/      Standalone Playwright HTTP server (port 18790)
├── sessions/     JSONL conversation history store
└── ui/           index.html — single-file vanilla JS chat UI

tests/            197 tests, fully mocked (pytest + pytest-asyncio)
```

## Running tests

```bash
uv run pytest              # all 197 tests
uv run pytest -v           # verbose
uv run pytest tests/test_agent_pipeline.py  # one file
```

## Architecture notes

- **Gateway and browser server are isolated** — the gateway never imports Playwright. It calls the browser server over HTTP (`aiohttp`). The browser can crash and restart independently.
- **Double-response agent pattern** — the `agent` RPC sends an immediate `accepted` ack, then fires an `asyncio.Task` for the pipeline. Tokens fan out via an in-process event emitter → broadcaster → WebSocket.
- **Screenshots use Anthropic vision** — PNG bytes are forwarded as base64 image blocks (not text), so Claude actually sees the page.

## Stack

| Layer | Library |
|-------|---------|
| Web framework | FastAPI + uvicorn |
| WebSocket | Starlette native |
| AI provider | `anthropic` SDK (streaming) |
| Browser | Playwright (async, Chromium) |
| HTTP client | aiohttp |
| Config | Pydantic v2 + pydantic-settings |
| Tests | pytest + pytest-asyncio |
| Package manager | uv |
