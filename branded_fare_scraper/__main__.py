"""CLI entry point:  python -m branded_fare_scraper --input jobs.xlsx"""

from __future__ import annotations

import argparse
import sys

from .config import Config
from .models import Season
from .runner import run


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
    args = p.parse_args(argv)

    season_map = {"summer": Season.SUMMER, "winter": Season.WINTER}
    seasons = [season_map[s.strip().lower()] for s in args.seasons.split(",") if s.strip() in season_map]

    return Config(
        input_path=args.input, output_dir=args.output, concurrency=args.concurrency,
        browser_pool_size=args.concurrency, seasons=seasons or [Season.SUMMER, Season.WINTER],
        sources=[s.strip() for s in args.sources.split(",") if s.strip()],
        max_retries=args.max_retries, headless=args.headless, fresh=args.fresh,
        seed=args.seed, log_level=args.log_level,
    )


def main(argv: list[str] | None = None) -> int:
    cfg = build_config(argv)
    summary = run(cfg)
    return 0 if summary.total_normalized_rows > 0 or summary.total_units == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
