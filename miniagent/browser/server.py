from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from .context import BrowserContext
from .routes import register_browser_routes


def create_browser_app(ctx: BrowserContext) -> FastAPI:
    """
    Standalone FastAPI app for browser control on BROWSER_PORT (default 18790).

    OpenClaw equivalent: src/browser/server.ts — standalone Express server
    on controlPort. The gateway never imports this — it calls it via HTTP.

    Lifespan: starts BrowserContext on startup, stops on shutdown.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await ctx.start()
        yield
        await ctx.stop()

    app = FastAPI(
        title="miniagent browser server",
        version="0.1.0",
        lifespan=lifespan,
    )

    def get_ctx() -> BrowserContext:
        return ctx

    register_browser_routes(app, get_ctx)

    return app
