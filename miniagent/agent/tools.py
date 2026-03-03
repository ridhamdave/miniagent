"""
Tool definitions for Claude + BrowserClient (aiohttp HTTP).

OpenClaw builds tool definitions in src/browser/pw-ai.ts and src/agents/agent-scope.ts.
miniagent hard-codes 6 browser tools for clarity.

BrowserClient is an aiohttp HTTP client wrapping the browser server (runs on its own port).
The agent calls the browser as a tool via HTTP, so the browser can crash and restart
without taking down the gateway — the same isolation OpenClaw enforces.
"""

import aiohttp

from ..config.loader import get_config


def get_tool_definitions() -> list[dict]:
    """
    Tool definitions in Anthropic SDK format, passed as tools= to messages.create().
    Claude decides when to use these; the pipeline executes them via execute_tool().

    OpenClaw builds tool definitions in src/browser/pw-ai.ts and src/agents/agent-scope.ts.
    miniagent hard-codes 6 browser tools for clarity.
    """
    return [
        {
            "name": "navigate",
            "description": "Navigate the browser to a URL",
            "input_schema": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Full URL including https://"}
                },
                "required": ["url"],
            },
        },
        {
            "name": "screenshot",
            "description": "Take a screenshot of the current browser tab. Use this to see what the page looks like.",
            "input_schema": {"type": "object", "properties": {}},
        },
        {
            "name": "click",
            "description": "Click an element on the page",
            "input_schema": {
                "type": "object",
                "properties": {
                    "ref": {
                        "type": "string",
                        "description": "CSS or aria selector, e.g. 'button:has-text(\"Submit\")'",
                    },
                    "double_click": {"type": "boolean", "default": False},
                },
                "required": ["ref"],
            },
        },
        {
            "name": "type_text",
            "description": "Type text into an input element",
            "input_schema": {
                "type": "object",
                "properties": {
                    "ref": {"type": "string"},
                    "text": {"type": "string"},
                },
                "required": ["ref", "text"],
            },
        },
        {
            "name": "get_text",
            "description": "Get visible text from the page or a specific element",
            "input_schema": {
                "type": "object",
                "properties": {
                    "ref": {
                        "type": "string",
                        "description": "Optional CSS selector; omit for full page text",
                    },
                },
            },
        },
        {
            "name": "scroll",
            "description": "Scroll the page",
            "input_schema": {
                "type": "object",
                "properties": {
                    "direction": {
                        "type": "string",
                        "enum": ["up", "down", "left", "right"],
                    },
                    "amount": {
                        "type": "integer",
                        "default": 500,
                        "description": "Pixels to scroll",
                    },
                },
                "required": ["direction"],
            },
        },
    ]


async def execute_tool(tool_name: str, tool_input: dict) -> dict:
    """
    Execute a tool by proxying to the browser HTTP server.

    OpenClaw: gateway server-methods/browser.ts proxies browser.request RPC
    calls to the local browser HTTP server. miniagent's agent does the same
    directly from the pipeline via aiohttp.
    """
    cfg = get_config()
    client = BrowserClient(f"http://127.0.0.1:{cfg.browser.control_port}")
    try:
        return await client.call(tool_name, tool_input)
    finally:
        await client.close()


class BrowserClient:
    """
    aiohttp HTTP client wrapping the browser server.

    Each instance manages a single aiohttp.ClientSession. Call close() when done
    or use as a context manager in tests.
    """

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        self._session: aiohttp.ClientSession | None = None

    def _get_session(self) -> aiohttp.ClientSession:
        """Lazily create the aiohttp session."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        """Close the aiohttp session."""
        if self._session is not None and not self._session.closed:
            await self._session.close()
            self._session = None

    async def navigate(self, url: str) -> dict:
        """Navigate the browser to a URL."""
        session = self._get_session()
        async with session.post(f"{self.base_url}/navigate", json={"url": url}) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def screenshot(self) -> dict:
        """Take a screenshot of the current browser tab."""
        session = self._get_session()
        async with session.get(f"{self.base_url}/screenshot") as resp:
            resp.raise_for_status()
            return await resp.json()

    async def click(self, ref: str, double: bool = False) -> dict:
        """Click an element on the page."""
        session = self._get_session()
        async with session.post(
            f"{self.base_url}/click", json={"ref": ref, "double_click": double}
        ) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def type_text(self, ref: str, text: str) -> dict:
        """Type text into an input element."""
        session = self._get_session()
        async with session.post(
            f"{self.base_url}/type", json={"ref": ref, "text": text}
        ) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def get_text(self, ref: str | None = None) -> dict:
        """Get visible text from the page or a specific element."""
        session = self._get_session()
        params = {"ref": ref} if ref is not None else {}
        async with session.get(f"{self.base_url}/text", params=params) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def scroll(self, direction: str, amount: int) -> dict:
        """Scroll the page."""
        session = self._get_session()
        async with session.post(
            f"{self.base_url}/scroll", json={"direction": direction, "amount": amount}
        ) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def call(self, tool_name: str, tool_input: dict) -> dict:
        """
        Generic dispatch method — maps tool name to the appropriate HTTP call.
        Used by execute_tool() and for backwards-compatible unified interface.
        """
        route_map: dict[str, tuple[str, str]] = {
            "navigate": ("POST", "/navigate"),
            "screenshot": ("GET", "/screenshot"),
            "click": ("POST", "/click"),
            "type_text": ("POST", "/type"),
            "get_text": ("GET", "/text"),
            "scroll": ("POST", "/scroll"),
        }
        if tool_name not in route_map:
            raise ValueError(f"Unknown tool: {tool_name}")

        method, path = route_map[tool_name]
        url = self.base_url + path
        session = self._get_session()

        if method == "GET":
            params = tool_input if tool_input else None
            async with session.get(url, params=params) as resp:
                resp.raise_for_status()
                return await resp.json()
        else:
            async with session.post(url, json=tool_input) as resp:
                resp.raise_for_status()
                return await resp.json()
