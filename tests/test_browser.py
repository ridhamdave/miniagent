"""
Tests for miniagent/browser/ — routes, server, and context.

All Playwright interaction is mocked via unittest.mock.AsyncMock and MagicMock.
No real browser is launched.
"""

from __future__ import annotations

import base64
from unittest.mock import AsyncMock, MagicMock

import httpx
from fastapi import FastAPI
from httpx import ASGITransport

from miniagent.browser.context import BrowserContext
from miniagent.browser.routes import (
    ClickBody,
    NavigateBody,
    ScrollBody,
    TypeBody,
    register_browser_routes,
)
from miniagent.browser.server import create_browser_app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_mock_ctx() -> MagicMock:
    """Build a fully mocked BrowserContext — no Playwright involved."""
    ctx = MagicMock(spec=BrowserContext)

    # navigate
    ctx.navigate = AsyncMock(return_value={"ok": True, "url": "https://example.com", "title": "Example"})

    # screenshot — returns raw PNG bytes
    fake_png = b"\x89PNG\r\n"
    ctx.screenshot = AsyncMock(return_value=fake_png)

    # click
    ctx.click = AsyncMock(return_value=None)

    # type_text
    ctx.type_text = AsyncMock(return_value=None)

    # get_text
    ctx.get_text = AsyncMock(return_value="Hello world")

    # scroll
    ctx.scroll = AsyncMock(return_value=None)

    # get_page — returns a mock page
    mock_page = MagicMock()
    mock_page.url = "https://example.com"
    mock_page.title = AsyncMock(return_value="Example")
    ctx.get_page = AsyncMock(return_value=mock_page)

    return ctx


def make_test_app(ctx: BrowserContext) -> FastAPI:
    """Build a plain FastAPI app with routes mounted (no lifespan ctx.start/stop)."""
    app = FastAPI()
    register_browser_routes(app, lambda: ctx)
    return app


# ---------------------------------------------------------------------------
# Route handler isolation tests (inject mock ctx directly)
# ---------------------------------------------------------------------------

class TestNavigateRoute:
    async def test_navigate_calls_ctx_and_returns_shape(self) -> None:
        ctx = make_mock_ctx()
        app = make_test_app(ctx)

        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/navigate", json={"url": "https://example.com"})

        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["url"] == "https://example.com"
        ctx.navigate.assert_awaited_once_with("https://example.com")

    async def test_navigate_missing_url_returns_422(self) -> None:
        ctx = make_mock_ctx()
        app = make_test_app(ctx)

        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/navigate", json={})

        assert resp.status_code == 422


class TestScreenshotRoute:
    async def test_get_screenshot_returns_base64_png(self) -> None:
        ctx = make_mock_ctx()
        fake_png = b"\x89PNG\r\n"
        ctx.screenshot = AsyncMock(return_value=fake_png)
        app = make_test_app(ctx)

        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/screenshot")

        assert resp.status_code == 200
        body = resp.json()
        assert "image_b64" in body
        assert body["mime_type"] == "image/png"
        # Verify base64 decodes back to original bytes
        decoded = base64.b64decode(body["image_b64"])
        assert decoded == fake_png
        ctx.screenshot.assert_awaited_once()

    async def test_post_screenshot_returns_base64_png(self) -> None:
        ctx = make_mock_ctx()
        fake_png = b"\x89PNG\r\n"
        ctx.screenshot = AsyncMock(return_value=fake_png)
        app = make_test_app(ctx)

        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/screenshot")

        assert resp.status_code == 200
        body = resp.json()
        assert "image_b64" in body
        assert body["mime_type"] == "image/png"
        decoded = base64.b64decode(body["image_b64"])
        assert decoded == fake_png


class TestClickRoute:
    async def test_click_calls_ctx_with_ref(self) -> None:
        ctx = make_mock_ctx()
        app = make_test_app(ctx)

        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/click", json={"ref": "button#submit"})

        assert resp.status_code == 200
        assert resp.json() == {"ok": True}
        ctx.click.assert_awaited_once_with("button#submit", False)

    async def test_click_double_click_flag(self) -> None:
        ctx = make_mock_ctx()
        app = make_test_app(ctx)

        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/click", json={"ref": "button#submit", "double_click": True})

        assert resp.status_code == 200
        ctx.click.assert_awaited_once_with("button#submit", True)

    async def test_click_missing_ref_returns_422(self) -> None:
        ctx = make_mock_ctx()
        app = make_test_app(ctx)

        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/click", json={})

        assert resp.status_code == 422


class TestTypeRoute:
    async def test_type_text_calls_ctx(self) -> None:
        ctx = make_mock_ctx()
        app = make_test_app(ctx)

        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/type", json={"ref": "input#search", "text": "hello"})

        assert resp.status_code == 200
        assert resp.json() == {"ok": True}
        ctx.type_text.assert_awaited_once_with("input#search", "hello")

    async def test_type_missing_fields_returns_422(self) -> None:
        ctx = make_mock_ctx()
        app = make_test_app(ctx)

        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/type", json={"ref": "input#search"})

        assert resp.status_code == 422


class TestGetTextRoute:
    async def test_get_text_full_page(self) -> None:
        ctx = make_mock_ctx()
        ctx.get_text = AsyncMock(return_value="Full page text content")
        app = make_test_app(ctx)

        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/text")

        assert resp.status_code == 200
        assert resp.json() == {"text": "Full page text content"}
        ctx.get_text.assert_awaited_once_with(None)

    async def test_get_text_with_ref(self) -> None:
        ctx = make_mock_ctx()
        ctx.get_text = AsyncMock(return_value="Element text")
        app = make_test_app(ctx)

        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/text", params={"ref": "#main"})

        assert resp.status_code == 200
        assert resp.json() == {"text": "Element text"}
        ctx.get_text.assert_awaited_once_with("#main")


class TestScrollRoute:
    async def test_scroll_down(self) -> None:
        ctx = make_mock_ctx()
        app = make_test_app(ctx)

        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/scroll", json={"direction": "down", "amount": 300})

        assert resp.status_code == 200
        assert resp.json() == {"ok": True}
        ctx.scroll.assert_awaited_once_with("down", 300)

    async def test_scroll_default_amount(self) -> None:
        ctx = make_mock_ctx()
        app = make_test_app(ctx)

        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/scroll", json={"direction": "up"})

        assert resp.status_code == 200
        ctx.scroll.assert_awaited_once_with("up", 500)

    async def test_scroll_missing_direction_returns_422(self) -> None:
        ctx = make_mock_ctx()
        app = make_test_app(ctx)

        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/scroll", json={})

        assert resp.status_code == 422


class TestStatusRoute:
    async def test_status_returns_running_true(self) -> None:
        ctx = make_mock_ctx()
        app = make_test_app(ctx)

        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/status")

        assert resp.status_code == 200
        body = resp.json()
        assert body["running"] is True
        assert "url" in body
        assert "title" in body


# ---------------------------------------------------------------------------
# Full app integration tests via create_browser_app
# ---------------------------------------------------------------------------

class TestCreateBrowserApp:
    async def test_app_navigate_route(self) -> None:
        ctx = make_mock_ctx()
        # Patch start/stop so lifespan doesn't try to launch Playwright
        ctx.start = AsyncMock(return_value=None)
        ctx.stop = AsyncMock(return_value=None)
        app = create_browser_app(ctx)

        async with httpx.AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            resp = await client.post("/navigate", json={"url": "https://example.com"})

        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        ctx.navigate.assert_awaited_once_with("https://example.com")

    async def test_app_screenshot_route(self) -> None:
        ctx = make_mock_ctx()
        fake_png = b"\x89PNG\r\ntest"
        ctx.screenshot = AsyncMock(return_value=fake_png)
        ctx.start = AsyncMock(return_value=None)
        ctx.stop = AsyncMock(return_value=None)
        app = create_browser_app(ctx)

        async with httpx.AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            resp = await client.get("/screenshot")

        assert resp.status_code == 200
        body = resp.json()
        assert base64.b64decode(body["image_b64"]) == fake_png
        assert body["mime_type"] == "image/png"

    async def test_app_click_route(self) -> None:
        ctx = make_mock_ctx()
        ctx.start = AsyncMock(return_value=None)
        ctx.stop = AsyncMock(return_value=None)
        app = create_browser_app(ctx)

        async with httpx.AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            resp = await client.post("/click", json={"ref": "a.link"})

        assert resp.status_code == 200
        assert resp.json() == {"ok": True}
        ctx.click.assert_awaited_once_with("a.link", False)

    async def test_app_type_route(self) -> None:
        ctx = make_mock_ctx()
        ctx.start = AsyncMock(return_value=None)
        ctx.stop = AsyncMock(return_value=None)
        app = create_browser_app(ctx)

        async with httpx.AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            resp = await client.post("/type", json={"ref": "input", "text": "world"})

        assert resp.status_code == 200
        assert resp.json() == {"ok": True}
        ctx.type_text.assert_awaited_once_with("input", "world")

    async def test_app_text_route(self) -> None:
        ctx = make_mock_ctx()
        ctx.get_text = AsyncMock(return_value="page content")
        ctx.start = AsyncMock(return_value=None)
        ctx.stop = AsyncMock(return_value=None)
        app = create_browser_app(ctx)

        async with httpx.AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            resp = await client.get("/text")

        assert resp.status_code == 200
        assert resp.json() == {"text": "page content"}

    async def test_app_scroll_route(self) -> None:
        ctx = make_mock_ctx()
        ctx.start = AsyncMock(return_value=None)
        ctx.stop = AsyncMock(return_value=None)
        app = create_browser_app(ctx)

        async with httpx.AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            resp = await client.post("/scroll", json={"direction": "left", "amount": 200})

        assert resp.status_code == 200
        assert resp.json() == {"ok": True}
        ctx.scroll.assert_awaited_once_with("left", 200)

    async def test_app_lifespan_calls_start_and_stop(self) -> None:
        ctx = make_mock_ctx()
        ctx.start = AsyncMock(return_value=None)
        ctx.stop = AsyncMock(return_value=None)
        app = create_browser_app(ctx)

        # ASGITransport doesn't fire ASGI lifespan events — test the lifespan
        # context manager directly instead.
        async with app.router.lifespan_context(app):
            pass

        ctx.start.assert_awaited_once()
        ctx.stop.assert_awaited_once()


# ---------------------------------------------------------------------------
# BrowserContext unit tests (without Playwright)
# ---------------------------------------------------------------------------

class TestBrowserContextImport:
    def test_context_can_be_imported_without_playwright(self) -> None:
        """Importing BrowserContext should not raise even without Playwright installed."""
        from miniagent.browser.context import BrowserContext as BC

        ctx = BC()
        assert ctx._playwright is None
        assert ctx._browser is None
        assert ctx._page is None

    def test_page_property_returns_none_before_start(self) -> None:
        from miniagent.browser.context import BrowserContext as BC

        ctx = BC()
        assert ctx.page is None


class TestBrowserContextMethodsWithMocks:
    """Test BrowserContext methods by injecting mock Playwright objects."""

    async def test_navigate_calls_page_goto(self) -> None:
        from miniagent.browser.context import BrowserContext as BC

        ctx = BC()
        mock_page = MagicMock()
        mock_page.is_closed.return_value = False
        mock_page.goto = AsyncMock()
        mock_page.url = "https://example.com"
        mock_page.title = AsyncMock(return_value="Example Domain")
        ctx._page = mock_page
        ctx._context = MagicMock()

        result = await ctx.navigate("https://example.com")

        mock_page.goto.assert_awaited_once()
        assert result["ok"] is True
        assert result["url"] == "https://example.com"
        assert result["title"] == "Example Domain"

    async def test_screenshot_calls_page_screenshot(self) -> None:
        from miniagent.browser.context import BrowserContext as BC

        ctx = BC()
        mock_page = MagicMock()
        mock_page.is_closed.return_value = False
        fake_png = b"\x89PNG"
        mock_page.screenshot = AsyncMock(return_value=fake_png)
        ctx._page = mock_page
        ctx._context = MagicMock()

        result = await ctx.screenshot()

        mock_page.screenshot.assert_awaited_once_with(full_page=True)
        assert result == fake_png

    async def test_click_calls_locator_click(self) -> None:
        from miniagent.browser.context import BrowserContext as BC

        ctx = BC()
        mock_locator = MagicMock()
        mock_locator.click = AsyncMock()
        mock_page = MagicMock()
        mock_page.is_closed.return_value = False
        mock_page.locator.return_value = mock_locator
        ctx._page = mock_page
        ctx._context = MagicMock()

        await ctx.click("button#submit")

        mock_page.locator.assert_called_once_with("button#submit")
        mock_locator.click.assert_awaited_once()

    async def test_click_double_calls_dbl_click(self) -> None:
        from miniagent.browser.context import BrowserContext as BC

        ctx = BC()
        mock_locator = MagicMock()
        mock_locator.dbl_click = AsyncMock()
        mock_page = MagicMock()
        mock_page.is_closed.return_value = False
        mock_page.locator.return_value = mock_locator
        ctx._page = mock_page
        ctx._context = MagicMock()

        await ctx.click("button#submit", double_click=True)

        mock_locator.dbl_click.assert_awaited_once()

    async def test_type_text_calls_fill(self) -> None:
        from miniagent.browser.context import BrowserContext as BC

        ctx = BC()
        mock_locator = MagicMock()
        mock_locator.fill = AsyncMock()
        mock_page = MagicMock()
        mock_page.is_closed.return_value = False
        mock_page.locator.return_value = mock_locator
        ctx._page = mock_page
        ctx._context = MagicMock()

        await ctx.type_text("input#name", "Alice")

        mock_page.locator.assert_called_once_with("input#name")
        mock_locator.fill.assert_awaited_once()

    async def test_get_text_full_page_uses_body(self) -> None:
        from miniagent.browser.context import BrowserContext as BC

        ctx = BC()
        mock_page = MagicMock()
        mock_page.is_closed.return_value = False
        mock_page.inner_text = AsyncMock(return_value="body text")
        ctx._page = mock_page
        ctx._context = MagicMock()

        result = await ctx.get_text()

        mock_page.inner_text.assert_awaited_once_with("body")
        assert result == "body text"

    async def test_get_text_with_ref_uses_locator(self) -> None:
        from miniagent.browser.context import BrowserContext as BC

        ctx = BC()
        mock_locator = MagicMock()
        mock_locator.inner_text = AsyncMock(return_value="element text")
        mock_page = MagicMock()
        mock_page.is_closed.return_value = False
        mock_page.locator.return_value = mock_locator
        ctx._page = mock_page
        ctx._context = MagicMock()

        result = await ctx.get_text("#content")

        mock_page.locator.assert_called_once_with("#content")
        mock_locator.inner_text.assert_awaited_once()
        assert result == "element text"

    async def test_scroll_down_calls_mouse_wheel_with_positive_dy(self) -> None:
        from miniagent.browser.context import BrowserContext as BC

        ctx = BC()
        mock_mouse = MagicMock()
        mock_mouse.wheel = AsyncMock()
        mock_page = MagicMock()
        mock_page.is_closed.return_value = False
        mock_page.mouse = mock_mouse
        ctx._page = mock_page
        ctx._context = MagicMock()

        await ctx.scroll("down", 400)

        mock_mouse.wheel.assert_awaited_once_with(0, 400)

    async def test_scroll_up_calls_mouse_wheel_with_negative_dy(self) -> None:
        from miniagent.browser.context import BrowserContext as BC

        ctx = BC()
        mock_mouse = MagicMock()
        mock_mouse.wheel = AsyncMock()
        mock_page = MagicMock()
        mock_page.is_closed.return_value = False
        mock_page.mouse = mock_mouse
        ctx._page = mock_page
        ctx._context = MagicMock()

        await ctx.scroll("up", 300)

        mock_mouse.wheel.assert_awaited_once_with(0, -300)

    async def test_scroll_right_calls_mouse_wheel_with_positive_dx(self) -> None:
        from miniagent.browser.context import BrowserContext as BC

        ctx = BC()
        mock_mouse = MagicMock()
        mock_mouse.wheel = AsyncMock()
        mock_page = MagicMock()
        mock_page.is_closed.return_value = False
        mock_page.mouse = mock_mouse
        ctx._page = mock_page
        ctx._context = MagicMock()

        await ctx.scroll("right", 200)

        mock_mouse.wheel.assert_awaited_once_with(200, 0)

    async def test_get_page_creates_new_page_if_closed(self) -> None:
        from miniagent.browser.context import BrowserContext as BC

        ctx = BC()
        # Simulate a closed page
        closed_page = MagicMock()
        closed_page.is_closed.return_value = True

        new_page = MagicMock()
        mock_context = MagicMock()
        mock_context.new_page = AsyncMock(return_value=new_page)

        ctx._page = closed_page
        ctx._context = mock_context

        page = await ctx.get_page()

        assert page is new_page
        mock_context.new_page.assert_awaited_once()
