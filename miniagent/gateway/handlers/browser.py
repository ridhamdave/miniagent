"""
Browser handlers — proxy browser.* RPC calls to the browser HTTP server.

Each handler validates its params, then calls BrowserClient to proxy
the request to the browser server running on its own port.

OpenClaw equivalent: src/gateway/server-methods/browser.ts

The gateway NEVER imports playwright directly. All browser interaction
goes through HTTP calls to the browser server.
"""

from __future__ import annotations

from ...agent.tools import BrowserClient
from ...config.types import MiniAgentConfig
from ...protocol.error_codes import ErrorCode, error_shape
from ...protocol.methods import (
    BrowserClickParams,
    BrowserGetTextParams,
    BrowserNavigateParams,
    BrowserTypeParams,
)
from ..handler_registry import HandlerContext, HandlerFn


def make_browser_handlers(
    cfg: MiniAgentConfig,
    browser_base_url: str | None = None,
) -> dict[str, HandlerFn]:
    """
    Factory returns dict of browser.* handlers.
    Each handler proxies to the browser HTTP server via BrowserClient.

    Parameters
    ----------
    cfg:
        Full config (used to derive browser_base_url if not provided directly).
    browser_base_url:
        Base URL override. If None, derived from cfg.browser.control_port.
    """
    if browser_base_url is None:
        browser_base_url = f"http://127.0.0.1:{cfg.browser.control_port}"

    async def _call_browser(tool_name: str, tool_input: dict) -> dict:
        """Helper: create a BrowserClient, call, close, return result."""
        client = BrowserClient(browser_base_url)
        try:
            return await client.call(tool_name, tool_input)
        finally:
            await client.close()

    async def browser_navigate(ctx: HandlerContext) -> None:
        try:
            params = BrowserNavigateParams.model_validate(ctx.params)
        except Exception as e:
            await ctx.respond(False, None, error_shape(ErrorCode.INVALID_REQUEST, str(e)))
            return
        try:
            result = await _call_browser("navigate", {"url": params.url})
            await ctx.respond(True, result)
        except Exception as e:
            await ctx.respond(False, None, error_shape(ErrorCode.INTERNAL, str(e)))

    async def browser_screenshot(ctx: HandlerContext) -> None:
        try:
            result = await _call_browser("screenshot", {})
            await ctx.respond(True, result)
        except Exception as e:
            await ctx.respond(False, None, error_shape(ErrorCode.INTERNAL, str(e)))

    async def browser_click(ctx: HandlerContext) -> None:
        try:
            params = BrowserClickParams.model_validate(ctx.params)
        except Exception as e:
            await ctx.respond(False, None, error_shape(ErrorCode.INVALID_REQUEST, str(e)))
            return
        try:
            result = await _call_browser(
                "click", {"ref": params.ref, "double_click": params.double_click}
            )
            await ctx.respond(True, result)
        except Exception as e:
            await ctx.respond(False, None, error_shape(ErrorCode.INTERNAL, str(e)))

    async def browser_type(ctx: HandlerContext) -> None:
        try:
            params = BrowserTypeParams.model_validate(ctx.params)
        except Exception as e:
            await ctx.respond(False, None, error_shape(ErrorCode.INVALID_REQUEST, str(e)))
            return
        try:
            result = await _call_browser("type_text", {"ref": params.ref, "text": params.text})
            await ctx.respond(True, result)
        except Exception as e:
            await ctx.respond(False, None, error_shape(ErrorCode.INTERNAL, str(e)))

    async def browser_get_text(ctx: HandlerContext) -> None:
        try:
            params = BrowserGetTextParams.model_validate(ctx.params)
        except Exception as e:
            await ctx.respond(False, None, error_shape(ErrorCode.INVALID_REQUEST, str(e)))
            return
        try:
            input_dict: dict = {}
            if params.ref is not None:
                input_dict["ref"] = params.ref
            result = await _call_browser("get_text", input_dict)
            await ctx.respond(True, result)
        except Exception as e:
            await ctx.respond(False, None, error_shape(ErrorCode.INTERNAL, str(e)))

    async def browser_scroll(ctx: HandlerContext) -> None:
        # Scroll params: direction (required), amount (optional, default 500)
        direction = ctx.params.get("direction")
        if not direction:
            await ctx.respond(
                False,
                None,
                error_shape(ErrorCode.INVALID_REQUEST, "direction is required"),
            )
            return
        amount = ctx.params.get("amount", 500)
        try:
            result = await _call_browser("scroll", {"direction": direction, "amount": amount})
            await ctx.respond(True, result)
        except Exception as e:
            await ctx.respond(False, None, error_shape(ErrorCode.INTERNAL, str(e)))

    return {
        "browser.navigate": browser_navigate,
        "browser.screenshot": browser_screenshot,
        "browser.click": browser_click,
        "browser.type": browser_type,
        "browser.get_text": browser_get_text,
        "browser.scroll": browser_scroll,
    }
