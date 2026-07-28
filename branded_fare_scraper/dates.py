"""Season-aware random date generation.

Rules from the spec:
* Two scenarios per run: one Summer, one Winter.
* Summer  = Apr, May, Jun, Jul, Aug, Sep, Oct.
* Winter  = Nov, Dec, Jan, Feb, Mar.
* Return date is exactly ``departure + 3`` days.
* If a departure has no availability, the next 7 days are tried automatically,
  first-found wins. So we emit an 8-entry window (D0 .. D0+7).
* Every fresh run must produce new random dates (a *resumed* run reuses the
  frozen plan — that is handled in ``checkpoint``/``runner``, not here).
"""

from __future__ import annotations

import random
from datetime import date, timedelta

from .models import DatePlan, Season

SUMMER_MONTHS = {4, 5, 6, 7, 8, 9, 10}
WINTER_MONTHS = {11, 12, 1, 2, 3}

DEFAULT_MIN_LEAD_DAYS = 21     # don't scrape dates too close in (airlines gate)
DEFAULT_HORIZON_DAYS = 300     # how far ahead we're willing to look
DEFAULT_TRIP_LENGTH = 3        # return = departure + 3
DEFAULT_WINDOW_DAYS = 7        # fallback window after the target departure


def _season_months(season: Season) -> set[int]:
    return SUMMER_MONTHS if season == Season.SUMMER else WINTER_MONTHS


def pick_departure(
    season: Season,
    today: date | None = None,
    min_lead_days: int = DEFAULT_MIN_LEAD_DAYS,
    horizon_days: int = DEFAULT_HORIZON_DAYS,
    rng: random.Random | None = None,
) -> date:
    """Pick a random departure date whose month is in ``season``.

    Searches ``[today+min_lead, today+horizon]`` so the year boundary (winter
    spanning Dec->Mar) is handled naturally. We keep the window (departure+7)
    inside the horizon.
    """
    today = today or date.today()
    rng = rng or random.Random()
    months = _season_months(season)
    start = today + timedelta(days=min_lead_days)
    end = today + timedelta(days=horizon_days)

    candidates = [
        start + timedelta(days=i)
        for i in range((end - start).days + 1)
        if (start + timedelta(days=i)).month in months
    ]
    if not candidates:
        # Extremely unlikely (horizon always spans >1 year of months); widen.
        end2 = today + timedelta(days=max(horizon_days, 400))
        candidates = [
            start + timedelta(days=i)
            for i in range((end2 - start).days + 1)
            if (start + timedelta(days=i)).month in months
        ]
    return rng.choice(candidates)


def build_date_plan(
    season: Season,
    today: date | None = None,
    *,
    min_lead_days: int = DEFAULT_MIN_LEAD_DAYS,
    horizon_days: int = DEFAULT_HORIZON_DAYS,
    trip_length: int = DEFAULT_TRIP_LENGTH,
    window_days: int = DEFAULT_WINDOW_DAYS,
    rng: random.Random | None = None,
) -> DatePlan:
    """Build a full ``DatePlan`` (target dates + fallback window) for a season."""
    departure = pick_departure(season, today, min_lead_days, horizon_days, rng)
    return_date = departure + timedelta(days=trip_length)
    window = [
        (departure + timedelta(days=i), departure + timedelta(days=i + trip_length))
        for i in range(window_days + 1)   # D0 .. D0+7  -> 8 candidates
    ]
    return DatePlan(season=season, departure=departure, return_date=return_date, window=window)


def build_all_plans(
    seasons: list[Season],
    today: date | None = None,
    rng: random.Random | None = None,
    **kwargs,
) -> dict[Season, DatePlan]:
    return {s: build_date_plan(s, today, rng=rng, **kwargs) for s in seasons}
