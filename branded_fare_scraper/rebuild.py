"""Reconstruct model objects from a persisted ``raw_data.jsonl``.

Single source of truth for turning raw JSONL back into ``RawBrand`` /
``CabinResult`` / ``UnitResult``. Used by the runner's resume-merge and by the
offline exporters (``reprocess_raw``, ``to_platform``, ``make_excel``) so the
raw→brand reconstruction lives in exactly one place.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Iterator, Optional

from .models import (AmenityStatus, Cabin, CabinResult, PriceType, RawAmenity, RawBrand,
                     RawMiles, ScrapeUnit, UnitResult, UnitStatus)

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
    )


def iter_raw_records(path: Path) -> Iterator[dict]:
    with Path(path).open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


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
