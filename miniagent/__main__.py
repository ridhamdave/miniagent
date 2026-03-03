"""
Entry point for `python -m miniagent` and `miniagent start` CLI.

Starts both servers concurrently in the same process:
  - Gateway  on config.gateway.port  (default 18789) — WebSocket + HTTP
  - Browser  on config.browser.control_port  (default 18790) — Playwright HTTP

Both share the same asyncio event loop via asyncio.gather().
"""

import asyncio

import uvicorn

from .browser.context import BrowserContext
from .browser.server import create_browser_app
from .config import get_config
from .gateway import create_gateway_app


async def _run_both() -> None:
    config = get_config()

    # Browser server — Playwright Chromium on its own port
    browser_ctx = BrowserContext()
    browser_app = create_browser_app(browser_ctx)
    browser_server = uvicorn.Server(
        uvicorn.Config(
            browser_app,
            host="127.0.0.1",
            port=config.browser.control_port,
            log_level=config.log_level,
        )
    )

    # Gateway — WebSocket + HTTP UI on the main port
    gateway_app = create_gateway_app(
        browser_base_url=f"http://127.0.0.1:{config.browser.control_port}"
    )
    gateway_server = uvicorn.Server(
        uvicorn.Config(
            gateway_app,
            host=config.gateway.host,
            port=config.gateway.port,
            log_level=config.log_level,
        )
    )

    # Run both servers concurrently; either can exit to stop both
    await asyncio.gather(
        browser_server.serve(),
        gateway_server.serve(),
    )


def main() -> None:
    asyncio.run(_run_both())


if __name__ == "__main__":
    main()
