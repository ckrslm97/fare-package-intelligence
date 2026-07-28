"""Airline Branded Fares Scraper — enterprise-grade, extensible scraping engine.

This package produces the most accurate *raw* branded-fare data possible and
converts it to a standard, flat schema. It deliberately does **not** perform
fare-family mapping — that is a downstream stage.

High level layout:
    models          Data model (dataclasses + enums).
    config          Runtime configuration.
    dates           Season-aware random date generation + fallback windows.
    normalization   Brand-name normalization + true hierarchy ordering.
    pricing         Display price + calculated absolute price (delta handling).
    amenities       Canonical amenity taxonomy + status classification.
    retry           Exponential-backoff retry with error classification.
    checkpoint      Frozen run-plan + resume support.
    validation      Post-scrape completeness validation.
    browser_pool    Reused Playwright contexts with a concurrency cap.
    io_utils        Excel/CSV/JSON input & output.
    logging_setup   Human log + structured JSONL event log.
    sources/        Pluggable per-source adapters (registry + priority).
    runner          Async orchestrator.
"""

from __future__ import annotations

__version__ = "1.0.0"
__all__ = ["__version__"]
