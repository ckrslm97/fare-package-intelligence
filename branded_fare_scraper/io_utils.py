"""Input reading + output writing.

Input: an Excel (``.xlsx``) or ``.csv`` with (at least) Carrier, Origin,
Destination columns (case-insensitive; a few common aliases accepted).

Outputs (all under the output dir):
* ``raw_data.jsonl``        — raw scrape result per unit
* ``normalized_data.csv``   — flat rows, one column per amenity (always written)
* ``normalized_data.xlsx``  — same, if openpyxl is available
* ``failed_jobs.csv``       — units that failed / had no availability
* ``summary.json``          — run summary

openpyxl is imported lazily so the engine has no hard third-party dependency for
CSV-only workflows.
"""

from __future__ import annotations

import csv
import json
import re
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterable

from .airports import meta as airport_meta
from .amenities import AMENITY_DISPLAY, AMENITY_KEYS
from .models import BrandedFare, Job, RunSummary, UnitResult

# --------------------------------------------------------------------------- #
# Input
# --------------------------------------------------------------------------- #
_CARRIER_ALIASES = {"carrier", "airline", "carriercode", "carrier_code", "crr", "taşıyıcı", "tasiyici"}
_ORIGIN_ALIASES = {"origin", "orgn", "from", "dep", "departure", "o", "kalkış", "kalkis"}
_DEST_ALIASES = {"destination", "dest", "to", "arrival", "arr", "d", "varış", "varis"}
_OND_ALIASES = {"ond", "route", "od", "o-d"}


def _match(colname: str, aliases: set[str]) -> bool:
    return colname.strip().lower().replace(" ", "") in aliases


def _split_ond(ond: str) -> tuple[str, str] | None:
    """Split 'LHR-SIN' (or 'LHR/SIN') into (origin, destination)."""
    parts = re.split(r"[-/_>]", str(ond).strip().upper())
    parts = [p for p in parts if p]
    if len(parts) == 2 and all(2 <= len(p) <= 4 for p in parts):
        return parts[0], parts[1]
    return None


def _rows_to_jobs(headers: list[str], rows: Iterable[list[Any]]) -> list[Job]:
    idx = {"carrier": None, "origin": None, "destination": None, "ond": None}
    for i, h in enumerate(headers):
        h = str(h or "")
        if idx["carrier"] is None and _match(h, _CARRIER_ALIASES):
            idx["carrier"] = i
        elif idx["origin"] is None and _match(h, _ORIGIN_ALIASES):
            idx["origin"] = i
        elif idx["destination"] is None and _match(h, _DEST_ALIASES):
            idx["destination"] = i
        elif idx["ond"] is None and _match(h, _OND_ALIASES):
            idx["ond"] = i

    if idx["carrier"] is None:
        raise ValueError(f"Input is missing a Carrier column. Headers seen: {headers}")
    has_od = idx["origin"] is not None and idx["destination"] is not None
    if not has_od and idx["ond"] is None:
        raise ValueError(f"Input needs either Origin+Destination or an OND column. Headers: {headers}")

    jobs: list[Job] = []
    seen: set[tuple[str, str, str]] = set()
    for row in rows:
        if not row or all(c in (None, "") for c in row):
            continue
        raw_row = {str(headers[i]): row[i] for i in range(min(len(headers), len(row)))}
        carrier = str(row[idx["carrier"]]).strip()
        if has_od and row[idx["origin"]] and row[idx["destination"]]:
            origin = str(row[idx["origin"]]).strip().upper()
            destination = str(row[idx["destination"]]).strip().upper()
        elif idx["ond"] is not None:
            split = _split_ond(row[idx["ond"]])
            if not split:
                continue
            origin, destination = split
        else:
            continue
        if not (carrier and origin and destination):
            continue
        key = (carrier.upper(), origin, destination)
        if key in seen:                      # dedupe repeated (carrier, OND) rows
            continue
        seen.add(key)
        jobs.append(Job(carrier=carrier, origin=origin, destination=destination, raw_row=raw_row))
    return jobs


def read_jobs(path: str) -> list[Job]:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Input file not found: {p}")
    if p.suffix.lower() in (".xlsx", ".xlsm"):
        from openpyxl import load_workbook  # lazy
        wb = load_workbook(p, read_only=True, data_only=True)
        try:
            ws = wb.active
            data = list(ws.iter_rows(values_only=True))
        finally:
            wb.close()
        if not data:
            return []
        headers = [str(c) if c is not None else "" for c in data[0]]
        return _rows_to_jobs(headers, [list(r) for r in data[1:]])
    # CSV
    with p.open("r", encoding="utf-8-sig", newline="") as f:
        reader = list(csv.reader(f))
    if not reader:
        return []
    return _rows_to_jobs(reader[0], reader[1:])


# --------------------------------------------------------------------------- #
# Output schema
# --------------------------------------------------------------------------- #
BASE_COLUMNS = [
    "Carrier", "Origin", "Destination",
    "Origin City Code", "Origin Country Code", "Dest City Code", "Dest Country Code",
    "Departure Date", "Return Date", "Season",
    "Cabin", "Brand Order", "Tier Code", "Raw Brand Name", "Normalized Brand Name",
    "Display Price", "Calculated Absolute Price", "Currency", "Source",
    "Fare Family Code", "Brand Description",
]
AMENITY_COLUMNS = [AMENITY_DISPLAY[k] for k in AMENITY_KEYS]
TAIL_COLUMNS = [
    "Mileage Available", "Miles Earned", "Bonus Percent",
    "Scrape Timestamp", "Status", "Retry Count",
]
ALL_COLUMNS = BASE_COLUMNS + AMENITY_COLUMNS + TAIL_COLUMNS


def flatten_branded_fare(bf: BrandedFare) -> dict[str, Any]:
    o_meta = airport_meta(bf.origin)
    d_meta = airport_meta(bf.destination)
    row: dict[str, Any] = {
        "Carrier": bf.carrier,
        "Origin": bf.origin,
        "Destination": bf.destination,
        "Origin City Code": o_meta["city_code"],
        "Origin Country Code": o_meta["country_code"],
        "Dest City Code": d_meta["city_code"],
        "Dest Country Code": d_meta["country_code"],
        "Departure Date": bf.departure_date.isoformat() if bf.departure_date else "",
        "Return Date": bf.return_date.isoformat() if bf.return_date else "",
        "Season": bf.season.value,
        "Cabin": bf.cabin.value,
        "Brand Order": bf.brand_order,
        "Tier Code": bf.tier_code,
        "Raw Brand Name": bf.raw_brand_name,
        "Normalized Brand Name": bf.normalized_brand_name,
        "Display Price": bf.display_price,
        "Calculated Absolute Price": bf.calculated_absolute_price,
        "Currency": bf.currency or "",
        "Source": bf.source,
        "Fare Family Code": bf.fare_family_code or "",
        "Brand Description": bf.brand_description,
        "Mileage Available": ("Yes" if bf.mileage_available else "No") if bf.mileage_available is not None else "",
        "Miles Earned": bf.miles_earned if bf.miles_earned is not None else "",
        "Bonus Percent": bf.bonus_percent if bf.bonus_percent is not None else "",
        "Scrape Timestamp": bf.scrape_timestamp,
        "Status": bf.status,
        "Retry Count": bf.retry_count,
    }
    for key in AMENITY_KEYS:
        row[AMENITY_DISPLAY[key]] = bf.amenities.get(key, "Unknown")
    return row


def write_normalized(rows: list[BrandedFare], out_dir: Path) -> tuple[Path, Path | None]:
    flat = [flatten_branded_fare(r) for r in rows]
    csv_path = out_dir / "normalized_data.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=ALL_COLUMNS)
        w.writeheader()
        w.writerows(flat)

    xlsx_path: Path | None = None
    try:
        from openpyxl import Workbook  # lazy
        wb = Workbook()
        ws = wb.active
        ws.title = "Normalized"
        ws.append(ALL_COLUMNS)
        for row in flat:
            ws.append([row.get(c, "") for c in ALL_COLUMNS])
        ws.freeze_panes = "A2"
        xlsx_path = out_dir / "normalized_data.xlsx"
        wb.save(xlsx_path)
    except ImportError:
        xlsx_path = None                       # openpyxl not installed -> CSV is enough
    except Exception as e:  # noqa: BLE001 - real write error: warn, keep CSV
        import logging
        logging.getLogger("bfs").warning("xlsx write failed (%s); CSV written", e)
        xlsx_path = None
    return csv_path, xlsx_path


def write_raw(units: list[UnitResult], out_dir: Path) -> Path:
    path = out_dir / "raw_data.jsonl"
    with path.open("w", encoding="utf-8") as f:
        for u in units:
            rec = {
                "unit_key": u.unit.key(),
                "carrier": u.unit.job.carrier,
                "origin": u.unit.job.origin,
                "destination": u.unit.job.destination,
                "season": u.unit.date_plan.season.value,
                "source": u.source,
                "status": u.status.value,
                "retry_count": u.retry_count,
                "error": u.error,
                "elapsed_s": u.elapsed,
                "cabins": [
                    {
                        "cabin": c.cabin.value,
                        "source": c.source,
                        "departure": c.departure.isoformat() if c.departure else None,
                        "return": c.return_date.isoformat() if c.return_date else None,
                        "has_availability": c.has_availability,
                        "note": c.note,
                        "brands": [_raw_brand_to_dict(b) for b in c.brands],
                    }
                    for c in u.cabin_results
                ],
            }
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    return path


def _raw_brand_to_dict(b) -> dict[str, Any]:
    d = asdict(b)
    # enums -> values
    d["cabin"] = b.cabin.value
    d["price_type"] = b.price_type.value
    for a in d.get("amenities", []):
        a["status"] = a["status"].value if hasattr(a["status"], "value") else a["status"]
    return d


def write_failed(units: list[UnitResult], out_dir: Path) -> Path:
    path = out_dir / "failed_jobs.csv"
    cols = ["Carrier", "Origin", "Destination", "Season", "Status", "Retry Count",
            "Sources Tried", "Error", "Validation Issues", "Elapsed (s)"]
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for u in units:
            w.writerow({
                "Carrier": u.unit.job.carrier,
                "Origin": u.unit.job.origin,
                "Destination": u.unit.job.destination,
                "Season": u.unit.date_plan.season.value,
                "Status": u.status.value,
                "Retry Count": u.retry_count,
                "Sources Tried": ", ".join(u.sources_tried),
                "Error": u.error,
                "Validation Issues": "; ".join(u.validation_issues),
                "Elapsed (s)": u.elapsed,
            })
    return path


def write_summary(summary: RunSummary, out_dir: Path) -> Path:
    path = out_dir / "summary.json"
    path.write_text(json.dumps(summary.to_dict(), ensure_ascii=False, indent=2), "utf-8")
    return path
