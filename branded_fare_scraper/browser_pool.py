"""Reused-Playwright browser pool with a built-in concurrency cap.

One Chromium instance, N reused browser *contexts* (each an isolated session).
Acquiring a context blocks when all N are in use, so the pool itself enforces the
"at most 10 concurrent scraping tasks" rule — no separate semaphore needed
(though the runner keeps one for clarity/logging).

Playwright is imported lazily so importing the package never requires it.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Optional

from .config import Config


class BrowserPool:
    def __init__(self, config: Config):
        self.cfg = config
        self._pw = None
        self._browser = None
        self._idle: asyncio.Queue = asyncio.Queue()
        self._all_contexts: list = []
        self._started = False

    async def start(self) -> None:
        if self._started:
            return
        from playwright.async_api import async_playwright  # lazy import
        self._pw = await async_playwright().start()
        args = [
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-features=IsolateOrigins,site-per-process",
        ]
        if self.cfg.headless:
            # Headless HTTP/2 handshakes are rejected by some WAFs
            # (ERR_HTTP2_PROTOCOL_ERROR) and headless is often slow-walled;
            # forcing HTTP/1.1 helps. Headful behaves like a normal browser.
            args.append("--disable-http2")
        self._browser = await self._pw.chromium.launch(headless=self.cfg.headless, args=args)
        for _ in range(self.cfg.browser_pool_size):
            ctx = await self._new_context()
            self._all_contexts.append(ctx)
            self._idle.put_nowait(ctx)
        self._started = True

    async def _new_context(self):
        ctx = await self._browser.new_context(
            locale=self.cfg.locale,
            user_agent=self.cfg.user_agent,
            viewport={"width": 1366, "height": 900},
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
                "Upgrade-Insecure-Requests": "1",
            },
        )
        ctx.set_default_navigation_timeout(self.cfg.nav_timeout_ms)
        ctx.set_default_timeout(self.cfg.action_timeout_ms)
        # Light stealth: hide webdriver flag.
        await ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
        )
        return ctx

    @contextlib.asynccontextmanager
    async def page(self):
        """Acquire a fresh page on a reused context; auto-clean on release."""
        if not self._started:
            await self.start()
        ctx = await self._idle.get()
        page = None
        try:
            try:
                page = await ctx.new_page()
            except Exception:
                # Context is dead (e.g. renderer crash) — replace it so this and
                # later units don't all fail on a broken context.
                with contextlib.suppress(Exception):
                    await ctx.close()
                if ctx in self._all_contexts:
                    self._all_contexts.remove(ctx)
                ctx = await self._new_context()
                self._all_contexts.append(ctx)
                page = await ctx.new_page()
            yield page
        finally:
            with contextlib.suppress(Exception):
                if page is not None:
                    await page.close()
            # NOTE: intentionally do NOT clear cookies — wiping them drops the
            # Cloudflare clearance token (cf_clearance) and forces a fresh bot
            # challenge on every reuse. Searches are URL-driven, so retained
            # cookies are harmless and keep the context "warm".
            self._idle.put_nowait(ctx)

    async def stop(self) -> None:
        for ctx in self._all_contexts:
            with contextlib.suppress(Exception):
                await ctx.close()
        with contextlib.suppress(Exception):
            if self._browser:
                await self._browser.close()
        with contextlib.suppress(Exception):
            if self._pw:
                await self._pw.stop()
        self._started = False
        self._all_contexts.clear()


class NullPool:
    """Pool stand-in for adapters that need no browser (e.g. pure-API sources)."""

    @contextlib.asynccontextmanager
    async def page(self):
        yield None

    async def start(self) -> None:  # pragma: no cover
        pass

    async def stop(self) -> None:  # pragma: no cover
        pass
