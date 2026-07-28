"""Regenerate normalized_data + report from an existing raw_data.jsonl, applying
the current normalization/mapping — no re-scraping. Useful after tweaking the
amenity logic. Usage: python reprocess_raw.py <output_dir>
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

from branded_fare_scraper.amenities import empty_amenity_map
from branded_fare_scraper.io_utils import write_normalized
from branded_fare_scraper.models import AmenityStatus, BrandedFare, Cabin, PriceType, Season
from branded_fare_scraper.normalization import iter_ranked_by_cabin, tier_code
from branded_fare_scraper.rebuild import iter_raw_records, raw_brand_from_dict
from branded_fare_scraper.report import write_report

_RANK = {AmenityStatus.INCLUDED: 3, AmenityStatus.PAID: 2,
         AmenityStatus.NOT_INCLUDED: 1, AmenityStatus.UNKNOWN: 0}


def main() -> int:
    # Statuses are already resolved at scrape time (the Ubfly "plain line = Included"
    # rule lives in the adapter), so reprocessing must NOT re-upgrade Unknown here —
    # it would wrongly promote legitimately-unknown rights from other sources.
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "output")
    rows: list[BrandedFare] = []
    for rec in iter_raw_records(out / "raw_data.jsonl"):
        season = Season(rec["season"])
        for c in rec.get("cabins", []):
            brands = [raw_brand_from_dict(b) for b in c.get("brands", [])]
            if not brands:
                continue
            group_cabin = Cabin(c["cabin"])
            dep = date.fromisoformat(c["departure"]) if c.get("departure") else None
            ret = date.fromisoformat(c["return"]) if c.get("return") else None
            src = c.get("source") or rec.get("source", "")   # per-cabin source from raw
            for eff_cab, raw, nb, order, absp in iter_ranked_by_cabin(brands, group_cabin,
                                                                      carrier=rec["carrier"]):
                amap = empty_amenity_map(); details = {}
                for a in raw.amenities:
                    if not a.canonical_key or a.canonical_key not in amap:
                        continue
                    cur = AmenityStatus(amap[a.canonical_key])
                    if _RANK[a.status] >= _RANK[cur]:
                        amap[a.canonical_key] = a.status.value
                        details[a.canonical_key] = a.raw_value or ""   # overwrite (no stale)
                rows.append(BrandedFare(
                    carrier=rec["carrier"], origin=rec["origin"], destination=rec["destination"],
                    departure_date=dep, return_date=ret, season=season, cabin=eff_cab,
                    brand_order=order, tier_code=tier_code(eff_cab, order),
                    raw_brand_name=raw.raw_brand_name, normalized_brand_name=nb.normalized_name,
                    display_price=raw.display_price_text or "",
                    calculated_absolute_price=(absp if absp is not None
                                               else (raw.price_value if raw.price_type == PriceType.ABSOLUTE else None)),
                    currency=raw.currency, source=src,
                    fare_family_code=raw.fare_family_code, brand_description=raw.description,
                    amenities=amap, amenity_details=details,
                    mileage_available=raw.miles.mileage_available,
                    miles_earned=raw.miles.miles_earned, bonus_percent=raw.miles.bonus_percent,
                    status=rec.get("status", "success"), retry_count=rec.get("retry_count", 0)))
    csv_path, xlsx_path = write_normalized(rows, out)
    rep = write_report(rows, out)
    print(f"Reprocessed {len(rows)} rows -> {csv_path} (+{xlsx_path.name if xlsx_path else 'no xlsx'}) + {rep.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
