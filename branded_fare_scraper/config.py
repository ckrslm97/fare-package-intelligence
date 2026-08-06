"""Runtime configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .models import Season


@dataclass
class Config:
    # I/O
    input_path: str = "input.xlsx"
    output_dir: str = "output"

    # Which seasons to generate a scenario for.
    seasons: list[Season] = field(default_factory=lambda: [Season.SUMMER, Season.WINTER])

    # Concurrency
    concurrency: int = 10            # spec: at most 10 concurrent scraping tasks
    browser_pool_size: int = 10      # reused contexts; usually == concurrency

    # Retry
    max_retries: int = 3
    retry_base_delay: float = 2.0    # seconds; exponential backoff base
    retry_max_delay: float = 30.0

    # Date generation
    min_lead_days: int = 21
    horizon_days: int = 300
    trip_length: int = 3
    window_days: int = 6             # 7-day period: D0 .. D0+6 (runtime budget)
    #: 'far3' (default): deepest bookable band, 3 consecutive dates + ordered
    #: fallback blocks. 'window': legacy single-block walk of window_days+1.
    date_mode: str = "far3"
    #: far3 sampling band (days out) — the least-sold, fullest-ladder inventory.
    far_lead_min_days: int = 300
    far_lead_max_days: int = 330

    # Browser
    headless: bool = False           # airline sites are friendlier headful
    nav_timeout_ms: int = 60_000
    action_timeout_ms: int = 25_000
    locale: str = "en-US"
    user_agent: str = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
    #: Playwright browser channel ("chrome", "msedge", …). When set, the pool
    #: launches that REAL installed browser and keeps its own user agent — a
    #: stealth-sensitive source must not be handed a spoofed UA that
    #: contradicts the genuine Chrome fingerprint. None = today's behaviour
    #: (bundled Chromium + the UA above), which is what Ubfly uses.
    browser_channel: str | None = None
    #: Connect to a managed REMOTE Chrome over CDP instead of launching one
    #: locally (keeps the scrape alive when this laptop sleeps). Neutral
    #: transport only — no stealth/captcha/proxy options are ever added.
    cdp_endpoint: str | None = None
    #: Egress proxy for the launched browser, e.g. "http://user:pass@host:port"
    #: or "socks5://host:port". Needed because Trip.com's /flights/ section
    #: walls off flagged IPs outright (live-verified 2026-08-01: this laptop's
    #: home IP gets a "Challenge Validation" interstitial, so a direct run
    #: collects nothing). Playwright wants the credentials split out, which
    #: :meth:`proxy_options` does.
    proxy: str | None = None
    #: Drive the scrape through the browser-act CLI (a hosted private browser
    #: behind a rotating proxy) instead of a local Playwright browser. Set this
    #: to a browser id from `browser-act browser list` and the runner switches
    #: transports; everything else about the run is unchanged. Exists because a
    #: hosted browser has no CDP endpoint for Playwright to attach to.
    browseract_browser_id: str | None = None
    browseract_session_prefix: str = "bfs"
    #: Generous: each call is a process spawn plus a hosted round trip.
    browseract_timeout_s: float = 180.0
    #: Wait for network idle after each navigation. Trip.com never really goes
    #: idle, so this can cost more than letting the adapter poll for the rows it
    #: actually needs — measured per site, not assumed.
    #: Directory holding cookies/localStorage between runs. A bot check that a
    #: visitor PASSES issues a clearance cookie; launching a cold profile every
    #: run throws it away, so each run arrives as a brand-new visitor from the
    #: same IP. Ubfly served 482 flights to the first such visit on 2026-08-02
    #: and an interactive Cloudflare challenge a few visits later. Empty keeps
    #: the old throwaway-profile behaviour.
    storage_state_path: str = ""
    browseract_wait_stable: bool = True
    #: CSS selector that means "this page is ready" for the source being run.
    #: When set it REPLACES ``wait stable`` after a navigation: the hosted
    #: browser returns the moment the element appears instead of waiting for
    #: the whole SPA to go quiet. Measured on the 2026-08-02 Trip.com pilot,
    #: ``wait stable`` was 756 s of the run's 1003 s — 75% of a bill that is
    #: charged by hosted-browser time, for a signal the caller then re-derives
    #: by polling anyway. Empty keeps the old behaviour.
    browseract_ready_selector: str = ""

    # Per-source politeness. Trip.com rate-limits a wide pool hitting one host
    # ("too many attempts" + slide puzzle), so it gets its own, narrower limits.
    tripcom_min_interval_s: float = 4.0
    source_concurrency: int = 2
    #: Ubfly is a gentle TOP-UP source only — its own, slower pace.
    ubfly_min_interval_s: float = 6.0

    # Source selection (adapter names). Empty => all registered, by priority.
    sources: list[str] = field(default_factory=list)

    # Carriers to add opportunistically to every OND (produce rows only where the
    # carrier actually appears in the OTA results). E.g. PC (Pegasus), VF (AJet).
    # Round 15: emptied by default. On the 300-OND long-haul list PC/VF added
    # ~1,180 units (35% of the run) for almost no rows; pass
    # --opportunistic PC,VF to bring them back.
    opportunistic_carriers: list[str] = field(default_factory=list)

    # Run control
    fresh: bool = False              # ignore checkpoint, generate a new plan
    passengers: int = 1
    run_id: str = field(default_factory=lambda: datetime.now().strftime("%Y%m%d_%H%M%S"))
    log_level: str = "INFO"
    seed: int | None = None          # for reproducible date generation (tests)

    # Post-scrape validation retry pass
    validation_retry: bool = True

    #: Opt-in (default off): fill a cabin's missing tiers from OTHER dates
    #: already walked in the same window instead of discarding every date but
    #: the single strongest one. 2026-08-03 pilot feature — off by default so
    #: today's selection behaviour (and every test asserting it) is unchanged
    #: until the small-sample trial is reviewed.
    merge_cross_date_ladders: bool = False

    @property
    def out(self) -> Path:
        return Path(self.output_dir)

    @property
    def state_dir(self) -> Path:
        return self.out / "state"

    @property
    def logs_dir(self) -> Path:
        return self.out / "logs"

    def ensure_dirs(self) -> None:
        for d in (self.out, self.state_dir, self.logs_dir):
            d.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_env(cls) -> "Config":
        c = cls()
        if v := os.getenv("BFS_INPUT"):
            c.input_path = v
        if v := os.getenv("BFS_OUTPUT"):
            c.output_dir = v
        if v := os.getenv("BFS_CONCURRENCY"):
            c.concurrency = int(v)
        if v := os.getenv("BFS_HEADLESS"):
            c.headless = v.lower() in ("1", "true", "yes")
        if v := os.getenv("BFS_PROXY"):
            c.proxy = v
        if v := os.getenv("BFS_BROWSERACT_ID"):
            c.browseract_browser_id = v
        return c

    def proxy_options(self) -> dict | None:
        """``proxy=`` kwarg for Playwright's launch, or None when unset.

        Playwright takes the username/password as separate fields, so inline
        credentials in the URL are split out here rather than at each call site.
        """
        if not self.proxy:
            return None
        from urllib.parse import urlsplit, urlunsplit
        p = urlsplit(self.proxy)
        host = p.hostname or ""
        if p.port:
            host = f"{host}:{p.port}"
        opts = {"server": urlunsplit((p.scheme, host, "", "", ""))}
        if p.username:
            opts["username"] = p.username
        if p.password:
            opts["password"] = p.password
        return opts
