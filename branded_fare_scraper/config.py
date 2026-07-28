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
    window_days: int = 7

    # Browser
    headless: bool = False           # airline sites are friendlier headful
    nav_timeout_ms: int = 60_000
    action_timeout_ms: int = 25_000
    locale: str = "en-US"
    user_agent: str = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )

    # Source selection (adapter names). Empty => all registered, by priority.
    sources: list[str] = field(default_factory=list)

    # Carriers to add opportunistically to every OND (produce rows only where the
    # carrier actually appears in the OTA results). E.g. PC (Pegasus), VF (AJet).
    opportunistic_carriers: list[str] = field(default_factory=lambda: ["PC", "VF"])

    # Run control
    fresh: bool = False              # ignore checkpoint, generate a new plan
    passengers: int = 1
    run_id: str = field(default_factory=lambda: datetime.now().strftime("%Y%m%d_%H%M%S"))
    log_level: str = "INFO"
    seed: int | None = None          # for reproducible date generation (tests)

    # Post-scrape validation retry pass
    validation_retry: bool = True

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
        return c
