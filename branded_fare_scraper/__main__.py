"""CLI entry point:  python -m branded_fare_scraper --input jobs.xlsx"""

from __future__ import annotations

import argparse
import os
import sys

from .config import Config
from .models import Season
from .runner import run


def _far_lead(spec: str) -> tuple[int, int]:
    """Parse "300:330" into (min, max) days out."""
    try:
        lo, hi = (int(x) for x in str(spec).split(":", 1))
    except (TypeError, ValueError):
        raise SystemExit(f"--far-lead expects MIN:MAX in days, got {spec!r}")
    if lo < 1 or hi < lo:
        raise SystemExit(f"--far-lead range is not sane: {spec!r}")
    return lo, hi


def build_config(argv: list[str] | None = None) -> Config:
    p = argparse.ArgumentParser(
        prog="branded_fare_scraper",
        description="Enterprise airline branded-fare scraper (real data, official sites first).",
    )
    p.add_argument("--input", "-i", default="input.xlsx", help="Input .xlsx/.csv (Carrier, Origin, Destination).")
    p.add_argument("--output", "-o", default="output", help="Output directory.")
    p.add_argument("--concurrency", "-c", type=int, default=10, help="Max concurrent units (default 10).")
    p.add_argument("--seasons", default="summer,winter",
                   help="Comma list of seasons to scrape (summer,winter).")
    p.add_argument("--sources", default="", help="Comma list of source names to restrict to.")
    p.add_argument("--max-retries", type=int, default=3)
    p.add_argument("--headless", action="store_true", help="Run browser headless (default: headful).")
    p.add_argument("--fresh", action="store_true", help="Ignore checkpoint; generate new random dates.")
    p.add_argument("--seed", type=int, default=None, help="Seed for reproducible date generation.")
    p.add_argument("--log-level", default="INFO")
    p.add_argument("--opportunistic", default="",
                   help="Comma-separated carriers to add to every OND (default: none).")
    # Trip.com serves a degraded, never-hydrating page to Playwright's bundled
    # Chromium, so the pool launches real Chrome by default; Ubfly works on it
    # too. Pass --channel "" to force the bundled browser.
    p.add_argument("--date-mode", default="far3", choices=["far3", "window"],
                   help="far3 (default): deepest bookable band, 3 dates + fallback "
                        "blocks; window: legacy single-block walk.")
    p.add_argument("--far-lead", default="300:330", metavar="MIN:MAX",
                   help="far3 sampling band in days out (default 300:330).")
    p.add_argument("--source-concurrency", type=int, default=2,
                   help="Max workers allowed on a rate-limited source (Trip.com) at once.")
    p.add_argument("--tripcom-interval", type=float, default=4.0,
                   help="Minimum seconds between Trip.com page hits, globally.")
    p.add_argument("--cdp-endpoint", default=None, metavar="URL",
                   help="Connect to a remote Chrome over CDP instead of launching "
                        "locally, e.g. a Browserless standard-route endpoint "
                        "wss://production-lon.browserless.io?token=...&timeout=300000")
    p.add_argument("--channel", default="chrome",
                   help='Playwright browser channel ("chrome" default; "" = bundled Chromium).')
    p.add_argument("--proxy", default=os.getenv("BFS_PROXY"), metavar="URL",
                   help="Egress proxy for the browser, e.g. "
                        "http://user:pass@host:port or socks5://host:port. "
                        "Use when this machine's IP is walled off by the target. "
                        "Defaults to $BFS_PROXY.")
    p.add_argument("--browseract-id", default=os.getenv("BFS_BROWSERACT_ID"),
                   metavar="ID",
                   help="Run through the browser-act CLI using this hosted browser "
                        "(see `browser-act browser list`) instead of a local "
                        "Playwright browser. Its own proxy applies, so --proxy is "
                        "ignored. Defaults to $BFS_BROWSERACT_ID.")
    p.add_argument("--storage-state", default="", metavar="FILE",
                   help="Carry cookies between runs via this file. A bot check "
                        "that a visitor passes grants a clearance cookie; a cold "
                        "profile every run throws it away and arrives as a brand "
                        "new visitor. Suggested: .cache/profile.json")
    p.add_argument("--merge-cross-date", action="store_true",
                   help="Pilot (2026-08-03, off by default): fill a cabin's missing "
                        "tiers from OTHER dates already walked in the same window "
                        "instead of keeping only the single strongest date.")
    p.add_argument("--browseract-ready", default="", metavar="CSS",
                   help="After each navigation wait for this selector instead of "
                        "network-idle. Hosted-browser time is what costs credits "
                        "and 'wait stable' was 75%% of the 2026-08-02 pilot's bill "
                        "for a signal the adapter re-derives anyway. For Trip.com: "
                        "'[data-testid=\"u_select_btn\"]'.")
    args = p.parse_args(argv)

    season_map = {"summer": Season.SUMMER, "winter": Season.WINTER}
    seasons = [season_map[s.strip().lower()] for s in args.seasons.split(",") if s.strip() in season_map]

    return Config(
        input_path=args.input, output_dir=args.output, concurrency=args.concurrency,
        browser_pool_size=args.concurrency, seasons=seasons or [Season.SUMMER, Season.WINTER],
        sources=[s.strip() for s in args.sources.split(",") if s.strip()],
        max_retries=args.max_retries, headless=args.headless, fresh=args.fresh,
        seed=args.seed, log_level=args.log_level,
        browser_channel=(args.channel or None),
        cdp_endpoint=(args.cdp_endpoint or None),
        proxy=(args.proxy or None),
        browseract_browser_id=(args.browseract_id or None),
        browseract_ready_selector=args.browseract_ready,
        storage_state_path=args.storage_state,
        source_concurrency=args.source_concurrency,
        tripcom_min_interval_s=args.tripcom_interval,
        date_mode=args.date_mode,
        far_lead_min_days=_far_lead(args.far_lead)[0],
        far_lead_max_days=_far_lead(args.far_lead)[1],
        merge_cross_date_ladders=args.merge_cross_date,
        opportunistic_carriers=[c.strip().upper()
                                for c in args.opportunistic.split(",") if c.strip()],
    )


def main(argv: list[str] | None = None) -> int:
    cfg = build_config(argv)
    summary = run(cfg)
    return 0 if summary.total_normalized_rows > 0 or summary.total_units == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
    
