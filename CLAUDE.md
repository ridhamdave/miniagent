# miniagent — Claude Code Guide

This file is automatically read by Claude Code at the start of every session and injected into every sub-agent. Keep it accurate and concise.

---

## What This Project Is

A simplified personal AI gateway mirroring OpenClaw's core architecture.
- **Single provider**: Anthropic Claude (claude-sonnet-4-6 default, claude-opus-4-6 for heavy reasoning)
- **Single channel**: Browser Web UI via WebSocket
- **Browser control**: Playwright Chromium (separate process on its own port)
- **Language**: Python — FastAPI, asyncio, Pydantic v2, pydantic-settings

Full design spec: `DESIGN.md` — read it before touching any module.

---

## Architecture in One Screen

```
Browser UI  ──WS──►  Gateway :18789  ──asyncio.Task──►  Claude API
                          │                                   │
                          │◄──── streaming tokens ────────────┘
                          │
                          └──HTTP──►  Browser Server :18790  ──►  Playwright Chromium
```

Key isolation rule: **the browser server is always a separate process/port**. The gateway never imports browser code directly — it calls it via HTTP (aiohttp).

---

## Module Map

```
miniagent/
├── config/     — Pydantic config types, env settings, singleton loader
├── protocol/   — RequestFrame, ResponseFrame, EventFrame, error codes
├── gateway/    — FastAPI WebSocket server, connection lifecycle, RPC handlers
│   └── handlers/ — agent.py, chat.py, browser.py
├── agent/      — AgentPipeline (streaming + tool loop), tools, events
├── browser/    — Standalone FastAPI HTTP server + Playwright context + routes
├── sessions/   — JSONL session store
└── ui/         — index.html (single file, no build step)

tests/
├── test_protocol.py      — Pydantic round-trip
├── test_gateway.py       — WS TestClient RPC shapes
├── test_agent_pipeline.py — Mock Anthropic SDK, assert event order
└── test_browser.py       — Mock BrowserContext, assert route shapes
```

---

## Code Conventions

- **Python 3.12+** — use `X | Y` unions, `match` statements, `type` aliases
- **Pydantic v2** — `model_validate`, `model_dump`, `Field(default_factory=...)`
- **async everywhere** — all I/O is `async def`. No `time.sleep`, no blocking calls.
- **No global mutable state** except the config singleton (`get_config()`) and the event bus
- **Type annotations on every function signature** — return types included
- **Imports**: stdlib → third-party → local. Never `from module import *`.
- **Error handling**: raise typed exceptions at domain boundaries; let FastAPI handle HTTP errors
- **Tests**: pytest + pytest-asyncio. Every public function needs at least one test.

---

## Development Workflow

### Running the project
```bash
uv run python -m miniagent          # start gateway + browser server
uv run pytest                        # run all tests
uv run pytest tests/test_protocol.py # run one test file
```

### Adding a new module
1. Write the Pydantic types / interfaces first
2. Write the test file (even if tests are stubs)
3. Implement the module
4. Run tests — fix until green

### Environment
- Copy `.env.example` → `.env`, set `MINIAGENT_ANTHROPIC_API_KEY`
- Config priority: env vars > config.yaml > Pydantic defaults

---

## Sub-Agent Workflow (Parallel Vibe Coding)

This project is designed so modules can be built in parallel because they communicate only through typed interfaces.

### When to use sub-agents
- Implementing independent modules (config, protocol, browser server, agent pipeline)
- Writing tests for a module while another agent implements it
- Exploring DESIGN.md to answer architecture questions while main session codes

### How to scope a sub-agent task
Give each agent:
1. **The module it owns** (exact file paths)
2. **Its interface contract** (what it imports, what imports it)
3. **The DESIGN.md section** it should read
4. **Whether to write or just research** — be explicit

### Module independence map (what can run in parallel)
```
config/     ← no internal deps, start here
protocol/   ← no internal deps, start here
sessions/   ← only imports config/
browser/    ← only imports config/
agent/      ← imports config/, protocol/
gateway/    ← imports everything; implement last
ui/         ← standalone HTML, any time
```

### Worktrees for true isolation
```bash
# Claude Code can run agents in isolated git worktrees:
# When launching an agent, set isolation: "worktree"
# This gives each agent its own branch so they don't clobber each other
```

---

## Memory & Context Management

**MEMORY.md path** (absolute — works from any worktree):
```
/Users/ridhamdave/.claude/projects/-Users-ridhamdave-repos-open-agent-miniagent/memory/MEMORY.md
```

### Rules every agent must follow

1. **Before starting work**: Read MEMORY.md. Check the Implementation State table to avoid duplicating work.
2. **After finishing work**: Update MEMORY.md — this is mandatory, not optional.
3. **DESIGN.md is the source of truth** — do not paraphrase it, link to it.

### What to write to MEMORY.md when done

Update the Implementation State table row for your module:
```
| `module/` | complete | brief note on any non-obvious choices |
```

If you deviated from DESIGN.md (different type names, different file split, etc.), append to the **Deviations** section:
```
- `module/file.py`: reason for deviation
```

If you hit a design ambiguity that future agents should know about, append to **Key Decisions**.

---

## Testing Strategy

| Layer | Tool | What to mock |
|-------|------|--------------|
| config | pytest | env vars via monkeypatch |
| protocol | pytest | nothing — pure Pydantic |
| gateway WS | pytest + httpx WS TestClient | Anthropic SDK |
| agent pipeline | pytest-asyncio | Anthropic SDK (return fake stream) |
| browser routes | pytest | BrowserContext (inject mock) |
| browser server | pytest + httpx | Playwright via BrowserContext mock |

Never make real Anthropic API calls in tests. Use `unittest.mock.AsyncMock`.

---

## Current Implementation State

> See MEMORY.md for up-to-date status. DESIGN.md is the spec.
> Nothing is implemented yet — only DESIGN.md exists.
