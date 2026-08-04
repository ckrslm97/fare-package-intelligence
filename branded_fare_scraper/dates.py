"""Season-aware random date generation.

Rules from the spec:
* Two scenarios per run: one Summer, one Winter.
* Summer  = Apr, May, Jun, Jul, Aug, Sep, Oct.
* Winter  = Nov, Dec, Jan, Feb, Mar.
* Return date is exactly ``departure + 3`` days.
* The full period is the departure date plus the next 14 days, so we emit a
  15-entry window (D0 .. D0+14). Every date is inspected (see
  ``SourceAdapter.run_unit``): the richest ladder in the period wins, rather
  than the first date that happens to sell.
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
DEFAULT_WINDOW_DAYS = 6        # full period after the target departure (D0..D0+6)

# ---- far-future mode (round 16 default) ----------------------------------- #
#: Far-out departures are still unsold, so the airline normally still displays
#: its COMPLETE branded-fare ladder; close-in dates have the cheap families sold
#: out and show a truncated, misleading one.
#: Deepest bookable band: airlines load schedules ~330-360 days out, so this is
#: about as far as inventory exists — and it is the least-sold, so the whole
#: fare-family ladder is normally still on sale.
FAR_MIN_LEAD_DAYS = 300
FAR_MAX_LEAD_DAYS = 330
#: Ordered fallback bands (centre, +/- spread) for units the deepest window
#: cannot serve: at 300+ days some carriers have no schedule loaded yet.
#:
#: The ~90-day band is the last resort and exists because "carrier not on route"
#: was never a statement about the route — only about one lead time. The
#: 2026-08-02 run reported GQ absent from FRA-ATH and DE absent from FRA-BKK
#: while the operator's market data says both serve those ONDs; small and
#: seasonal carriers simply load and sell much later than the 300-330 day band
#: this study is built around. A ladder captured at ~90 days can be truncated
#: (the cheapest families start selling out), which is exactly why it is tried
#: LAST and never in place of a deeper block that worked.
FAR_FALLBACK_LEADS = ((210, 20), (150, 20), (90, 20))
#: Raised 2 -> 4 on 2026-08-04. Each extra date is another chance to catch a
#: complete ladder, and the per-(OND, date, cabin) search cache means one date
#: costs ONE page load for the whole OND no matter how many carriers list it.
FAR_WINDOW_DAYS = 4            # departure + the next four days -> 5 candidates
FAR_BLOCK = FAR_WINDOW_DAYS + 1
MODE_FAR3 = "far3"
MODE_WINDOW = "window"


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
    mode: str = MODE_FAR3,
    far_lead: tuple[int, int] | None = None,
    rng: random.Random | None = None,
) -> DatePlan:
    """Build a ``DatePlan`` (target date + candidate blocks) for a season.

    ``mode="far3"`` (the default from round 16) samples as FAR OUT as inventory
    exists: a departure in the deepest bookable band (300-330 days by default,
    tunable via ``far_lead``) plus the next two days. The reasoning is
    commercial, not technical — a far-future flight is essentially unsold, so
    every branded-fare family is still on sale and one drawer shows the
    airline's complete ladder, whereas close-in dates have the cheap families
    sold out and read as a short ladder that understates the product. Three
    consecutive dates give redundancy against a single bad day, and the run
    keeps the richest ladder per cabin (``ladder_strength``).

    Because airlines only load schedules ~330-360 days out, some carriers have
    nothing at the deepest band, so the plan carries ORDERED FALLBACK BLOCKS at
    roughly 210 and 150 days. ``DatePlan.blocks`` exposes them; an adapter walks
    block 2 only when block 1 produced nothing for that unit, so a healthy unit
    never pays for the extra dates.

    Season wins over lead time: if no month of the season falls inside a
    requested band (e.g. a Winter scenario generated in January), the search
    widens rather than returning an out-of-season date.

    ``mode="window"`` keeps the previous behaviour (one long walk from a
    3-week-out departure) so older lists can be re-run unchanged.
    """
    if mode == MODE_FAR3:
        return _far3_plan(season, today, trip_length, far_lead, rng)
    departure = pick_departure(season, today, min_lead_days, horizon_days, rng)
    return_date = departure + timedelta(days=trip_length)
    window = [
        (departure + timedelta(days=i), departure + timedelta(days=i + trip_length))
        for i in range(window_days + 1)
    ]
    return DatePlan(season=season, departure=departure, return_date=return_date,
                    window=window, block_size=0)


def _band_pick(season: Season, today: date, lo: int, hi: int,
               rng: random.Random, block: int = 1) -> date | None:
    """A season-correct date whose lead time is within [lo, hi] days (or None).

    ``block`` is how many consecutive days will be walked from the returned
    date. The WHOLE block has to stay inside the season: picking only the
    start in-season was safe while a block was 3 days, but a longer block
    started late in a season month runs past its boundary and would price a
    "Summer" unit on September dates. Preferred candidates therefore keep the
    entire block in season; if the band is too narrow for that (a season edge),
    the start-only rule is used rather than giving up the band entirely.
    """
    months = _season_months(season)
    # Candidate starts may begin up to block-1 days BEFORE the band: the band
    # is a target for the block as a whole, and allowing the start to back off
    # by a few days is what lets a block finish inside the season when the
    # band only clips a season's last days (300-330 days from 2026-01-01 lands
    # on 28-31 October, where every 5-day block would otherwise spill into
    # November — i.e. into Winter).
    days = [today + timedelta(days=i)
            for i in range(max(1, lo - (block - 1)), hi + 1)]
    whole = [d for d in days
             if all((d + timedelta(days=k)).month in months for k in range(block))]
    in_season = [d for d in days if d.month in months and (today + timedelta(days=lo)) <= d]
    cands = whole or in_season
    return rng.choice(cands) if cands else None


def _far3_plan(season: Season, today: date | None, trip_length: int,
               far_lead: tuple[int, int] | None,
               rng: random.Random | None) -> DatePlan:
    """Deepest-window 3-date block plus its ordered fallback blocks."""
    today = today or date.today()
    rng = rng or random.Random()
    lo, hi = far_lead or (FAR_MIN_LEAD_DAYS, FAR_MAX_LEAD_DAYS)
    bands = [(lo, hi)] + [(c - s, c + s) for c, s in FAR_FALLBACK_LEADS]

    starts: list[date] = []
    for b_lo, b_hi in bands:
        d = _band_pick(season, today, b_lo, b_hi, rng, FAR_BLOCK)
        if d is None:                      # season beats lead time: widen
            d = _band_pick(season, today, max(1, b_lo - 90), b_hi + 90, rng, FAR_BLOCK)
        if d is not None and d not in starts:
            starts.append(d)
    if not starts:                         # pathological input: fall back wide
        starts = [pick_departure(season, today, DEFAULT_MIN_LEAD_DAYS,
                                 DEFAULT_HORIZON_DAYS, rng)]

    window: list[tuple[date, date]] = []
    for start in starts:
        window += [(start + timedelta(days=i), start + timedelta(days=i + trip_length))
                   for i in range(FAR_BLOCK)]
    departure = starts[0]
    return DatePlan(season=season, departure=departure,
                    return_date=departure + timedelta(days=trip_length),
                    window=window, block_size=FAR_BLOCK)


def build_all_plans(
    seasons: list[Season],
    today: date | None = None,
    rng: random.Random | None = None,
    **kwargs,
) -> dict[Season, DatePlan]:
    return {s: build_date_plan(s, today, rng=rng, **kwargs) for s in seasons}
