from __future__ import annotations

import asyncio


class Renderer:
    """Render public JavaScript pages using an ordinary isolated Chromium context."""

    def __init__(self):
        self.playwright = None
        self.browser = None
        self.lock = asyncio.Lock()
        self.slots = asyncio.Semaphore(2)
        self.stats: dict[str, dict] = {}

    async def close(self):
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()

    async def render(self, url: str, *, selector: str | None = None,
                     next_selector: str | None = None, max_pages: int = 1) -> str:
        from .http import USER_AGENT, BlockedError

        async with self.lock:
            if self.browser is None:
                try:
                    from playwright.async_api import async_playwright
                except ImportError as exc:
                    raise RuntimeError("JavaScript rendering needs the browser extra and Chromium installation") from exc
                self.playwright = await async_playwright().start()
                self.browser = await self.playwright.chromium.launch(headless=True)
        async with self.slots:
            context = await self.browser.new_context(user_agent=USER_AGENT)
            page = await context.new_page()
            try:
                async def trim_assets(route):
                    if route.request.resource_type in {"image", "media", "font"}:
                        await route.abort()
                    else:
                        await route.continue_()

                await page.route("**/*", trim_assets)
                response = await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                if response and response.status in {401, 403, 429}:
                    raise BlockedError(f"Browser received HTTP {response.status} from {url}")
                title = (await page.title()).lower()
                if any(marker in title for marker in ("just a moment", "verify you are", "access denied", "checking your browser")):
                    raise BlockedError(f"Browser encountered an access-verification page at {url}")
                try:
                    if selector:
                        await page.wait_for_selector(selector, state="attached", timeout=18000)
                    else:
                        await page.wait_for_selector('main h1, article h1, script[type="application/ld+json"]', state="attached", timeout=12000)
                except TimeoutError:
                    pass
                except Exception as exc:
                    # Playwright's timeout is separate from asyncio's timeout.
                    if type(exc).__name__ != "TimeoutError":
                        raise
                snapshots = [await page.content()]
                if "verify you are human" in (await page.locator("body").inner_text()).lower()[:3000]:
                    raise BlockedError(f"Browser encountered human verification at {url}")
                limited = False
                for _ in range(max_pages - 1):
                    if not next_selector or not selector:
                        break
                    button = page.locator(next_selector).first
                    if not await button.count() or not await button.is_visible() or await button.is_disabled():
                        break
                    previous = await page.locator(selector).evaluate_all("els => els.map(e => e.href || e.textContent).join('|')")
                    await button.click(timeout=5000)
                    try:
                        await page.wait_for_function(
                            "args => Array.from(document.querySelectorAll(args.selector)).map(e => e.href || e.textContent).join('|') !== args.previous",
                            arg={"selector": selector, "previous": previous}, timeout=18000,
                        )
                    except Exception as exc:
                        if type(exc).__name__ != "TimeoutError":
                            raise
                        limited = True
                        break
                    snapshots.append(await page.content())
                if next_selector:
                    button = page.locator(next_selector).first
                    limited = limited or bool(await button.count() and await button.is_visible())
                self.stats[url] = {"pages": len(snapshots), "limited": limited}
                return "\n".join(snapshots)
            finally:
                await context.close()
