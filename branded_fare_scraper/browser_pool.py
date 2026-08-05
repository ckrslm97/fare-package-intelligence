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
import logging
import os
from typing import Optional

from .config import Config

_LOG = logging.getLogger("bfs")


def _endpoint_host(endpoint: str) -> str:
    """Host of a CDP endpoint — never the query string (it carries the token)."""
    try:
        from urllib.parse import urlparse
        return urlparse(str(endpoint)).hostname or "remote"
    except Exception:  # noqa: BLE001
        return "remote"

#: Anti-automation flags for the BUNDLED Chromium profile. A real installed
#: browser must never get these — see :func:`launch_options`.
_BUNDLED_ARGS = (
    "--disable-blink-features=AutomationControlled",
    "--no-sandbox",
    "--disable-features=IsolateOrigins,site-per-process",
)
_BUNDLED_HEADERS = {
    "Accept-Language": "en-US,en;q=0.9",
    "Upgrade-Insecure-Requests": "1",
}
#: Light stealth for the bundled profile: hide the webdriver flag.
_WEBDRIVER_PATCH = "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"

_VIEWPORT = {"width": 1366, "height": 900}


def launch_options(cfg: Config, channel_ok: bool) -> dict:
    """Playwright ``chromium.launch`` kwargs.

    ``channel_ok`` = we are driving a REAL installed browser (``channel=
    "chrome"``). Then the profile must be authentic and *nothing* custom may be
    added: Trip.com serves the degraded, never-hydrating page to a Chrome that
    carries the automation flags, so the channel launch passes no ``args`` at
    all (live-verified 2026-07-30 — a plain context hydrates, the flagged one
    does not). The bundled-Chromium path keeps the legacy flags unchanged.
    """
    # A proxy is transport, not fingerprint: it is safe on the authentic-channel
    # path too, where no custom args may be passed.
    proxy = cfg.proxy_options()
    if channel_ok and cfg.browser_channel:
        opts = {"headless": cfg.headless, "channel": cfg.browser_channel}
        if proxy:
            opts["proxy"] = proxy
        return opts
    args = list(_BUNDLED_ARGS)
    if cfg.headless:
        # Headless HTTP/2 handshakes are rejected by some WAFs
        # (ERR_HTTP2_PROTOCOL_ERROR) and headless is often slow-walled;
        # forcing HTTP/1.1 helps. Headful behaves like a normal browser.
        args.append("--disable-http2")
    opts = {"headless": cfg.headless, "args": args}
    if proxy:
        opts["proxy"] = proxy
    return opts


def authentic_profile(cfg: Config, channel_ok: bool) -> bool:
    """True when the browser owns its own fingerprint and we must not touch it.

    Two cases: a REAL installed channel (chrome), and a REMOTE browser reached
    over CDP — the remote service owns that profile entirely, so overriding its
    user agent or injecting scripts would only make it inconsistent.
    """
    return bool(cfg.cdp_endpoint) or bool(channel_ok and cfg.browser_channel)


def context_options(cfg: Config, channel_ok: bool) -> dict:
    """Playwright ``new_context`` kwargs.

    Real-channel mode gets locale + viewport ONLY: a spoofed user agent or an
    injected ``Accept-Language``/``Upgrade-Insecure-Requests`` pair contradicts
    the genuine Chrome fingerprint and is exactly what the degraded page keys
    on. The bundled profile keeps its UA override and headers.
    """
    if authentic_profile(cfg, channel_ok):
        return {"locale": cfg.locale, "viewport": dict(_VIEWPORT)}
    return {
        "locale": cfg.locale,
        "user_agent": cfg.user_agent,
        "viewport": dict(_VIEWPORT),
        "extra_http_headers": dict(_BUNDLED_HEADERS),
    }


def stealth_init_script(channel_ok: bool) -> Optional[str]:
    """Init script for the context, or None for an authentic real-browser run."""
    return None if channel_ok else _WEBDRIVER_PATCH


#: Page loads a single context serves before it is retired. Chosen to bound
#: renderer memory on multi-hour runs without paying for a new context often.
_RECYCLE_AFTER = 40


class BrowserPool:
    def __init__(self, config: Config):
        self.cfg = config
        self._pw = None
        self._browser = None
        self._idle: asyncio.Queue = asyncio.Queue()
        self._all_contexts: list = []
        self._started = False
        #: True once a REAL installed browser (channel) launched successfully.
        self._channel_ok = False
        #: True when we merely CONNECTED to a browser we do not own.
        self._remote = False
        self._seen_contexts: set = set()
        self._created_contexts: list = []
        #: page() calls served by each context, so a long run can retire one
        #: before it accumulates enough renderer memory to take the browser
        #: down with it (see _RECYCLE_AFTER).
        self._ctx_uses: dict = {}
        self._relaunch_lock = asyncio.Lock()
        self._relaunches = 0

    async def start(self) -> None:
        if self._started:
            return
        from playwright.async_api import async_playwright  # lazy import
        self._pw = await async_playwright().start()
        if self.cfg.cdp_endpoint:
            await self._connect_remote()
            for _ in range(self.cfg.browser_pool_size):
                ctx = await self._acquire_context()
                self._all_contexts.append(ctx)
                self._idle.put_nowait(ctx)
            self._started = True
            return
        self._channel_ok = bool(self.cfg.browser_channel)
        await self._launch_local()
        _LOG.info("Browser: local channel=%s proxy=%s",
                  self.cfg.browser_channel or "bundled Chromium",
                  _endpoint_host(self.cfg.proxy) if self.cfg.proxy else "none")
        for _ in range(self.cfg.browser_pool_size):
            ctx = await self._new_context()
            self._all_contexts.append(ctx)
            self._idle.put_nowait(ctx)
        self._started = True

    async def _launch_local(self) -> None:
        """Launch (or relaunch) the local browser, honouring the channel fallback."""
        try:
            self._browser = await self._pw.chromium.launch(
                **launch_options(self.cfg, self._channel_ok))
        except Exception as e:  # noqa: BLE001 - channel may not be installed
            if not self._channel_ok:
                raise
            _LOG.warning("Browser channel %r unavailable (%s) — falling back to bundled "
                         "Chromium; Trip.com may not hydrate.", self.cfg.browser_channel, e)
            # Fall back to the bundled profile EXACTLY as it behaves natively
            # (legacy args + UA override + headers + stealth script).
            self.cfg.browser_channel = None
            self._channel_ok = False
            self._browser = await self._pw.chromium.launch(
                **launch_options(self.cfg, False))

    async def _connect_remote(self) -> None:
        """Attach to a managed remote Chrome over CDP (never launch locally).

        A failure here is fatal on purpose: silently falling back to the local
        browser would leave the operator unsure which browser produced the data.
        """
        endpoint = self.cfg.cdp_endpoint
        try:
            self._browser = await self._pw.chromium.connect_over_cdp(endpoint)
        except Exception as e:  # noqa: BLE001 - surface an actionable message
            raise RuntimeError(
                f"Could not connect to the remote browser over CDP at "
                f"{_endpoint_host(endpoint)}: {e}. Check the endpoint URL, the token "
                f"and that the service is reachable; the run is stopping rather "
                f"than quietly using the local browser."
            ) from e
        self._remote = True
        self._channel_ok = True            # the remote profile is authentic
        _LOG.info("Browser: remote CDP %s", _endpoint_host(endpoint))

    async def _acquire_context(self):
        """Reuse a context the remote browser already exposes, else make one."""
        for ctx in list(getattr(self._browser, "contexts", None) or []):
            if id(ctx) not in self._seen_contexts:
                self._seen_contexts.add(id(ctx))
                return ctx                 # theirs: we must not close it later
        return await self._new_context()

    def _storage_file(self) -> Optional[str]:
        """Saved cookies to open the context with, if any exist yet."""
        path = (self.cfg.storage_state_path or "").strip()
        if not path:
            return None
        return path if os.path.exists(path) else None

    async def _new_context(self):
        opts = context_options(self.cfg, self._channel_ok)
        saved = self._storage_file()
        if saved:
            # Re-enter as the visitor we already were: a clearance cookie earned
            # once is worth more than any pacing tweak, because the check keys on
            # "new visitor", not on request rate.
            opts["storage_state"] = saved
        ctx = await self._browser.new_context(**opts)
        self._created_contexts.append(ctx)
        ctx.set_default_navigation_timeout(self.cfg.nav_timeout_ms)
        ctx.set_default_timeout(self.cfg.action_timeout_ms)
        script = stealth_init_script(self._channel_ok)
        if script:                    # never inject into a real-browser profile
            await ctx.add_init_script(script)
        return ctx

    async def _relaunch_browser(self) -> None:
        """Bring the pool back after the BROWSER itself died.

        Recreating a context is not enough when the browser process is gone:
        ``new_context`` then fails too and every remaining unit dies instantly
        with "Target page, context or browser has been closed". That is exactly
        what a 2026-08-04 overnight run hit — the browser went down mid-pass
        and 811 units failed in seconds, 431 of which had ALREADY produced good
        data earlier in the run. A remote CDP browser is never relaunched: we
        do not own it, and silently swapping in a local one would leave the
        operator unsure which browser produced the data.
        """
        if self._remote:
            raise RuntimeError("Remote CDP browser is gone; not substituting a local one")
        async with self._relaunch_lock:
            if self._browser is not None and self._browser.is_connected():
                return                      # another worker already fixed it
            self._relaunches += 1
            _LOG.warning("Browser is gone — relaunching (#%d) and rebuilding %d context(s)",
                         self._relaunches, self.cfg.browser_pool_size)
            with contextlib.suppress(Exception):
                await self._browser.close()
            self._all_contexts.clear()
            self._created_contexts.clear()
            self._ctx_uses.clear()
            while not self._idle.empty():    # drop handles to the dead browser
                self._idle.get_nowait()
            await self._launch_local()
            for _ in range(self.cfg.browser_pool_size):
                ctx = await self._new_context()
                self._all_contexts.append(ctx)
                self._idle.put_nowait(ctx)

    async def _fresh_context(self, ctx):
        """Retire ``ctx`` and return a replacement, relaunching if needed."""
        with contextlib.suppress(Exception):
            await ctx.close()
        if ctx in self._all_contexts:
            self._all_contexts.remove(ctx)
        self._ctx_uses.pop(id(ctx), None)
        if self._browser is None or not self._browser.is_connected():
            await self._relaunch_browser()
            return await self._idle.get()
        new_ctx = await self._new_context()
        self._all_contexts.append(new_ctx)
        return new_ctx

    @contextlib.asynccontextmanager
    async def page(self):
        """Acquire a fresh page on a reused context; auto-clean on release."""
        if not self._started:
            await self.start()
        ctx = await self._idle.get()
        page = None
        try:
            # A context that has served many page loads carries the renderer
            # memory of every one of them; retiring it on a schedule keeps a
            # long run from ending in a browser-wide crash.
            if self._ctx_uses.get(id(ctx), 0) >= _RECYCLE_AFTER:
                ctx = await self._fresh_context(ctx)
            try:
                page = await ctx.new_page()
            except Exception:
                # Context is dead (e.g. renderer crash) — or the whole browser
                # is. _fresh_context tells the two apart and recovers from both.
                ctx = await self._fresh_context(ctx)
                page = await ctx.new_page()
            self._ctx_uses[id(ctx)] = self._ctx_uses.get(id(ctx), 0) + 1
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
        # Save the visitor identity BEFORE tearing the contexts down: the
        # clearance cookie a passive bot check grants is only useful if the next
        # run can present it. One context is enough — they share the profile.
        path = (self.cfg.storage_state_path or "").strip()
        if path and self._created_contexts:
            with contextlib.suppress(Exception):
                os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
                await self._created_contexts[0].storage_state(path=path)
                _LOG.info("Saved browser storage state -> %s", path)

        # Only close what WE created: a remote browser (and any context it
        # already had) belongs to the service, not to this run.
        for ctx in self._all_contexts:
            if self._remote and ctx not in self._created_contexts:
                continue
            with contextlib.suppress(Exception):
                await ctx.close()
        with contextlib.suppress(Exception):
            if self._browser and not self._remote:
                await self._browser.close()
        with contextlib.suppress(Exception):
            if self._pw:
                await self._pw.stop()
        self._started = False
        self._all_contexts.clear()
        self._created_contexts.clear()
        self._seen_contexts.clear()


class NullPool:
    """Pool stand-in for adapters that need no browser (e.g. pure-API sources)."""

    @contextlib.asynccontextmanager
    async def page(self):
        yield None

    async def start(self) -> None:  # pragma: no cover
        pass

    async def stop(self) -> None:  # pragma: no cover
        pass
