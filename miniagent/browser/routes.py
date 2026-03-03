from __future__ import annotations

import base64
from typing import Callable

from fastapi import FastAPI
from pydantic import BaseModel

from .context import BrowserContext


class NavigateBody(BaseModel):
    url: str


class ClickBody(BaseModel):
    ref: str
    double_click: bool = False


class TypeBody(BaseModel):
    ref: str
    text: str


class ScrollBody(BaseModel):
    direction: str  # "up" | "down" | "left" | "right"
    amount: int = 500


def register_browser_routes(app: FastAPI, get_ctx: Callable[[], BrowserContext]) -> None:
    """
    REST API for browser control.
    OpenClaw: src/browser/routes/agent.ts + routes/basic.ts

    These are plain HTTP routes — not WebSocket. The gateway's browser.* RPC handlers
    proxy WebSocket calls to these endpoints via BrowserClient (aiohttp).
    """

    @app.get("/status")
    async def status() -> dict:
        ctx = get_ctx()
        page = await ctx.get_page()
        return {"running": True, "url": page.url, "title": await page.title()}

    @app.post("/navigate")
    async def navigate(body: NavigateBody) -> dict:
        return await get_ctx().navigate(body.url)

    @app.get("/screenshot")
    async def screenshot_get() -> dict:
        png = await get_ctx().screenshot()
        return {"image_b64": base64.b64encode(png).decode(), "mime_type": "image/png"}

    @app.post("/screenshot")
    async def screenshot_post() -> dict:
        png = await get_ctx().screenshot()
        return {"image_b64": base64.b64encode(png).decode(), "mime_type": "image/png"}

    @app.post("/click")
    async def click(body: ClickBody) -> dict:
        await get_ctx().click(body.ref, body.double_click)
        return {"ok": True}

    @app.post("/type")
    async def type_text(body: TypeBody) -> dict:
        await get_ctx().type_text(body.ref, body.text)
        return {"ok": True}

    @app.get("/text")
    async def get_text(ref: str | None = None) -> dict:
        return {"text": await get_ctx().get_text(ref)}

    @app.post("/scroll")
    async def scroll(body: ScrollBody) -> dict:
        await get_ctx().scroll(body.direction, body.amount)
        return {"ok": True}
