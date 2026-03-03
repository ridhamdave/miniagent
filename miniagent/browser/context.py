from __future__ import annotations

from ..config.loader import get_config


class BrowserContext:
    """
    Manages one Playwright Chromium instance.

    OpenClaw equivalent: src/browser/pw-session.ts + server-context.ts
    OpenClaw supports multiple "profiles" (multiple browser instances, different
    CDP ports, separate user data dirs). miniagent simplifies to one instance.

    State machine: stopped -> starting -> running -> stopped

    Playwright is imported lazily inside start() so this module can be imported
    in test environments that don't have Playwright installed.
    """

    def __init__(self) -> None:
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None

    async def start(self) -> None:
        """Launch Chromium. Called once at server startup."""
        from playwright.async_api import async_playwright  # lazy import

        cfg = get_config()
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=cfg.browser.headless)
        self._context = await self._browser.new_context()
        self._page = await self._context.new_page()

    async def stop(self) -> None:
        """Close browser gracefully. Called at server shutdown."""
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()

    async def get_page(self):
        """Returns current page; creates a new one if it was closed."""
        if not self._page or self._page.is_closed():
            self._page = await self._context.new_page()
        return self._page

    @property
    def page(self):
        """Returns the active page (synchronous accessor)."""
        return self._page

    async def navigate(self, url: str) -> dict:
        page = await self.get_page()
        await page.goto(url, timeout=get_config().browser.timeout_ms)
        return {"ok": True, "url": page.url, "title": await page.title()}

    async def screenshot(self) -> bytes:
        """Full-page PNG screenshot."""
        page = await self.get_page()
        return await page.screenshot(full_page=True)

    async def click(self, ref: str, double_click: bool = False) -> None:
        """Click element by Playwright locator string."""
        page = await self.get_page()
        locator = page.locator(ref)
        if double_click:
            await locator.dbl_click(timeout=get_config().browser.timeout_ms)
        else:
            await locator.click(timeout=get_config().browser.timeout_ms)

    async def type_text(self, ref: str, text: str) -> None:
        page = await self.get_page()
        await page.locator(ref).fill(text, timeout=get_config().browser.timeout_ms)

    async def get_text(self, ref: str | None = None) -> str:
        page = await self.get_page()
        if ref:
            return await page.locator(ref).inner_text(timeout=get_config().browser.timeout_ms)
        return await page.inner_text("body")

    async def scroll(self, direction: str, amount: int = 500) -> None:
        page = await self.get_page()
        dx = {"left": -amount, "right": amount}.get(direction, 0)
        dy = {"up": -amount, "down": amount}.get(direction, 0)
        await page.mouse.wheel(dx, dy)
