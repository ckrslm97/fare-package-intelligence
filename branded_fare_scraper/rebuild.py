"""Reconstruct model objects from a persisted ``raw_data.jsonl``.

Single source of truth for turning raw JSONL back into ``RawBrand`` /
``CabinResult`` / ``UnitResult``. Used by the runner's resume-merge and by the
offline exporters (``reprocess_raw``, ``to_platform``, ``make_excel``) so the
raw→brand reconstruction lives in exactly one place.

It also builds the cross-season brand-pair preferences (see
``normalization.cross_season_pair_prefs``) from either a raw file or in-memory
results, so the run's own ``normalized_data.csv`` and every later reprocessing
of the same raw data publish IDENTICAL ladders.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Iterable, Iterator, Optional

from .models import (AmenityStatus, Cabin, CabinResult, PriceType, RawAmenity, RawBrand,
                     RawMiles, ScrapeUnit, UnitResult, UnitStatus)
from .normalization import (brand_match_key, cross_season_pair_prefs,
                            iter_unit_ranked_by_cabin)

_STATUSES = {s.value for s in AmenityStatus}


def raw_amenity_from_dict(a: dict) -> RawAmenity:
    st = a.get("status")
    return RawAmenity(
        raw_label=a.get("raw_label", ""),
        status=AmenityStatus(st) if st in _STATUSES else AmenityStatus.UNKNOWN,
        raw_value=a.get("raw_value", ""),
        fee_amount=a.get("fee_amount"),
        fee_currency=a.get("fee_currency"),
        canonical_key=a.get("canonical_key"),
    )


def raw_brand_from_dict(d: dict) -> RawBrand:
    m = d.get("miles") or {}
    return RawBrand(
        raw_brand_name=d.get("raw_brand_name", ""),
        cabin=Cabin(d["cabin"]),
        screen_order=d.get("screen_order", 0),
        price_value=d.get("price_value"),
        price_type=PriceType(d.get("price_type", "absolute")),
        currency=d.get("currency"),
        display_price_text=d.get("display_price_text", ""),
        fare_family_code=d.get("fare_family_code"),
        description=d.get("description", ""),
        amenities=[raw_amenity_from_dict(a) for a in d.get("amenities", [])],
        miles=RawMiles(mileage_available=m.get("mileage_available"),
                       miles_earned=m.get("miles_earned"), bonus_percent=m.get("bonus_percent")),
        source=d.get("source", ""),          # absent in pre-round-16 raw files
    )


def cabin_result_from_dict(c: dict) -> CabinResult:
    return CabinResult(
        cabin=Cabin(c["cabin"]),
        departure=date.fromisoformat(c["departure"]) if c.get("departure") else None,
        return_date=date.fromisoformat(c["return"]) if c.get("return") else None,
        brands=[raw_brand_from_dict(b) for b in c.get("brands", [])],
        has_availability=c.get("has_availability", True),
        note=c.get("note", ""),
        source=c.get("source", ""),
        flight_no=c.get("flight_no", ""),
        booking_class=c.get("booking_class", ""),
        operating_carrier=c.get("operating_carrier", ""),
        is_codeshare=c.get("is_codeshare"),
        is_interline=c.get("is_interline"),
    )


def _record_strength(rec: dict) -> tuple:
    """(total packages, total amenity lines) — used to pick a winner when the
    same unit was scraped more than once (see iter_raw_records)."""
    pkgs = 0
    amenities = 0
    for c in rec.get("cabins", []):
        brands = c.get("brands", [])
        pkgs += len(brands)
        for b in brands:
            amenities += len(b.get("amenities", []))
    return (pkgs, amenities)


def iter_raw_records(path: Path) -> Iterator[dict]:
    """Yield one record per (carrier, origin, destination, season) — deduped.

    A night's raw_data.jsonl is often the concatenation of several passes
    (the main run, a gap-fill retry, a resumed half) — appended with plain
    `cat`, not merged. If the SAME unit was scraped in more than one pass
    (its input list overlapped with a retry list, or a unit that already had
    data got re-attempted for a different reason), the file holds it TWICE
    and nothing downstream deduped that: both ladders got published side by
    side, doubling every tier on screen (live-verified 2026-08-04, TK
    AMS-ATH Winter showed "Restricted" and "Flexible" each twice).

    The stronger record (more packages, tie-broken by more amenity detail,
    tie-broken by "scraped later in the file") wins; the other is dropped
    before anything downstream ever sees it — this is the one place both
    reprocess_raw.py and to_platform.py read raw_data.jsonl through, so the
    fix applies everywhere without each caller having to know about it.
    """
    best: dict[tuple, dict] = {}
    order: list[tuple] = []          # first-seen order, for stable output
    with Path(path).open(encoding="utf-8") as f:
        for i, line in enumerate(f):
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            key = (rec.get("carrier", ""), rec.get("origin", ""),
                  rec.get("destination", ""), rec.get("season", ""))
            cur = best.get(key)
            if cur is None:
                best[key] = rec
                order.append(key)
                continue
            # Tie -> the later pass wins: it is presumably the more recent
            # attempt (a retry exists BECAUSE the earlier one was suspect).
            if _record_strength(rec) >= _record_strength(cur):
                best[key] = rec
    for key in order:
        yield best[key]


# --------------------------------------------------------------------------- #
# Cross-season pair-order preferences (shared by all four exporters)
# --------------------------------------------------------------------------- #
def collect_ladders(items: Iterable[tuple], ladders: Optional[dict] = None) -> dict:
    """Build the ``cross_season_pair_prefs`` input from published ladders.

    ``items`` yields ``(carrier, origin, destination, season, cabin_results)``.
    Each cabin's ladder is produced EXACTLY as the exporters publish it — same
    unit-level dedupe, ``iter_unit_ranked_by_cabin`` without preferences (the
    preferences are what we are deriving) — keyed by the *effective* cabin, so
    the evidence comes from the ladders that really publish. The FIRST ladder
    seen per ``(carrier, origin, destination, cabin, season)`` wins.
    """
    out: dict = {} if ladders is None else ladders
    for carrier, origin, destination, season, cabin_results in items:
        season = getattr(season, "value", season)
        per_cabin: dict = {}
        for eff_cab, raw, _nb, _order, absp, _src in iter_unit_ranked_by_cabin(
                cabin_results, carrier=carrier):
            per_cabin.setdefault(eff_cab, []).append(
                (brand_match_key(raw.raw_brand_name), absp))
        for eff_cab, ladder in per_cabin.items():
            out.setdefault((carrier, origin, destination, eff_cab, season), ladder)
    return out


def season_pair_prefs(raw_path: Path) -> dict:
    """Cross-season pair preferences from a run's ``raw_data.jsonl``."""
    def _items():
        for rec in iter_raw_records(raw_path):
            if rec.get("status", "success") != UnitStatus.SUCCESS.value:
                continue
            yield (rec["carrier"], rec["origin"], rec["destination"], rec["season"],
                   [cabin_result_from_dict(c) for c in rec.get("cabins", [])])
    return cross_season_pair_prefs(collect_ladders(_items()))


def season_pair_prefs_from_results(results: Iterable[UnitResult]) -> dict:
    """Same preferences, from in-memory results (the runner's own write path).

    Safe to run before the exporting pass over the SAME objects:
    ``iter_ranked_by_cabin`` mutates brand names/cabins but is idempotent.
    """
    def _items():
        for r in results:
            if r is None or r.status.value != UnitStatus.SUCCESS.value:
                continue
            job = r.unit.job
            yield (job.carrier, job.origin, job.destination,
                   r.unit.date_plan.season, r.cabin_results)
    return cross_season_pair_prefs(collect_ladders(_items()))


def pair_prefs_for(prefs: dict, carrier: str, origin: str, destination: str) -> dict:
    """Per-cabin slice for ``iter_ranked_by_cabin(pair_prefs_by_cabin=…)``."""
    return {cab: prefs.get((carrier, origin, destination, cab), {}) for cab in Cabin}


def unit_result_from_raw(rec: dict, unit_by_key: dict[str, ScrapeUnit]) -> Optional[UnitResult]:
    """Rebuild a UnitResult from one raw record, given the frozen plan's units."""
    unit = unit_by_key.get(rec.get("unit_key", ""))
    if unit is None:
        return None
    status = rec.get("status", "success")
    try:
        st = UnitStatus(status)
    except ValueError:
        st = UnitStatus.SUCCESS
    return UnitResult(
        unit=unit,
        source=rec.get("source") or "",
        cabin_results=[cabin_result_from_dict(c) for c in rec.get("cabins", [])],
        status=st,
        retry_count=rec.get("retry_count", 0),
        error=rec.get("error", ""),
    )
