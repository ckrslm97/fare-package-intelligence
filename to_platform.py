"""Inject the scraped dataset into the fare-intelligence HTML template.

Reads output_<dir>/raw_data.jsonl (for amenity details) + normalized_data.csv
(for per-cabin source), maps to the platform's `fares[]` schema + FEATURE_META
keys, and replaces the template's EMBEDDED placeholder.

Usage: python to_platform.py <template.html> <out_dir> <output.html>
"""

from __future__ import annotations

import csv
import json
import re
import sys
from datetime import date, datetime
from pathlib import Path

from branded_fare_scraper.airports import meta as airport_meta
from branded_fare_scraper.models import AmenityStatus, Cabin
from branded_fare_scraper.normalization import iter_ranked_by_cabin, tier_code
from branded_fare_scraper.rebuild import iter_raw_records, raw_brand_from_dict
from branded_fare_scraper.report import CARRIER_NAMES

# my canonical amenity key -> platform FEATURE_META key
FEATURE_KEY = {
    "cabin_baggage": "baggage_cabin", "checked_baggage": "checked_baggage",
    "seat_selection": "seat_selection", "meal": "meal", "lounge_access": "lounge",
    "priority_boarding": "priority_boarding", "fast_track": "fast_track",
    "refund": "refund", "change": "change", "no_show_refund": "no_show_refund",
    "no_show_change": "no_show_change", "same_day_earlier_flight": "same_day_change",
    "wifi": "wifi", "extra_baggage": "extra_baggage",
    "sports_equipment": "sport_equipment", "pet": "pet",
}

LCC = {"PC", "VF", "TR", "U2", "FR", "W6", "W4", "W9", "G9", "J9", "6E", "XY", "ZF", "NO", "TU", "FZ", "4S"}


def _short_detail(key: str, raw: str) -> str:
    """Concise one-line detail so cells don't overflow the row."""
    raw = (raw or "").strip()
    if not raw:
        return ""
    if key in ("cabin_baggage", "checked_baggage", "extra_baggage"):
        m2 = re.search(r"(\d+)\s*[x×]\s*(\d+)", raw)         # pieces × kg
        if m2:
            return f"{m2.group(1)}×{m2.group(2)}kg"
        m1 = re.search(r"(\d+)\s*kg", raw, re.I)             # weight only
        if m1:
            return f"{m1.group(1)}kg"
        return ""                                            # e.g. "1 piece" -> just ✓
    if key in ("change", "refund", "no_show_refund", "no_show_change", "same_day_earlier_flight"):
        w = re.sub(r"\(.*", "", raw).strip().split()
        return (w[0] if w else "")[:12]
    return ""  # other rights: the ✓/€/— state colour already carries the meaning


def _features(raw):
    out = {}
    seen_rank = {}
    rank = {AmenityStatus.INCLUDED: 3, AmenityStatus.PAID: 2, AmenityStatus.NOT_INCLUDED: 1, AmenityStatus.UNKNOWN: 0}
    for a in raw.amenities:
        pk = FEATURE_KEY.get(a.canonical_key or "")
        if not pk or a.status == AmenityStatus.UNKNOWN:
            continue
        if pk in seen_rank and rank[a.status] <= seen_rank[pk]:
            continue
        seen_rank[pk] = rank[a.status]
        feat = {"state": a.status.value}
        det = _short_detail(a.canonical_key or "", a.raw_value)
        if det:
            feat["detail"] = det
        out[pk] = feat
    # miles feature from mileage flag
    if raw.miles.mileage_available is not None:
        out["miles"] = {"state": "Included" if raw.miles.mileage_available else "Not Included"}
        if raw.miles.miles_earned:
            out["miles"]["detail"] = f"{int(raw.miles.miles_earned)} mil"
    return out


def build_fares(out_dir: Path):
    now = datetime.now().replace(microsecond=0).isoformat()
    coll_month = datetime.now().strftime("%Y-%m")
    fares = []
    for rec in iter_raw_records(out_dir / "raw_data.jsonl"):
        carrier = rec["carrier"]; o = rec["origin"]; d = rec["destination"]
        season = rec["season"]
        mo = airport_meta(o); md = airport_meta(d)
        oc, oreg = mo["country_name"], mo["region"]
        dc, dreg = md["country_name"], md["region"]
        ctype = "Low Cost" if carrier.upper() in LCC else "Legacy"
        # Local if either endpoint's country is Turkey, else Beyond.
        ondtype = "Local" if "TR" in (mo["country_code"], md["country_code"]) else "Beyond"
        for c in rec.get("cabins", []):
            brands = [raw_brand_from_dict(b) for b in c.get("brands", [])]
            if not brands:
                continue
            dep = c.get("departure")
            src = c.get("source") or rec.get("source", "")   # per-cabin source from raw
            group_cabin = Cabin(c["cabin"])
            for eff_cab, raw, nb, order, absp in iter_ranked_by_cabin(brands, group_cabin,
                                                                      carrier=carrier):
                cur = (raw.currency or "USD").upper()
                price = absp if absp is not None else raw.price_value
                price_usd = price if cur == "USD" else None
                fares.append({
                    "coll_date": coll_month, "coll_season": season, "query_date": dep,
                    "collection_time": now, "airline": carrier,
                    "airline_name": CARRIER_NAMES.get(carrier.upper(), carrier),
                    "origin": o, "destination": d, "origin_country": oc, "dest_country": dc,
                    "origin_region": oreg, "dest_region": dreg, "region": oreg,
                    "origin_city_code": mo["city_code"], "origin_country_code": mo["country_code"],
                    "dest_city_code": md["city_code"], "dest_country_code": md["country_code"],
                    "ond_type": ondtype, "season": season, "carrier_type": ctype,
                    "cabin": eff_cab.value, "fare_brand": raw.raw_brand_name,
                    "brand_code": raw.fare_family_code or nb.subtier or "",
                    "std_tier": tier_code(eff_cab, order), "package_order": order + 1,
                    "price": price, "price_usd": price_usd,
                    "currency": cur, "travel_date": dep,
                    "features": _features(raw),
                    "source": src, "flight_no": None,
                })
    return fares


def main():
    template = Path(sys.argv[1])
    out_dir = Path(sys.argv[2])
    out_html = Path(sys.argv[3])
    fares = build_fares(out_dir)
    payload = {"generated_at": datetime.now().replace(microsecond=0).isoformat(),
               "count": len(fares), "fares": fares}
    embedded = json.dumps(payload, ensure_ascii=False)
    html = template.read_text(encoding="utf-8")

    # --- Fix 1: the detail view grouped by carrier|OND only, so both travel
    # seasons became duplicate columns. Split cards by season and label them.
    patches = [
        ('rows.forEach(f=>{ const k=f.airline+"|"+flowKey(f,pdDim);',
         'rows.forEach(f=>{ const k=f.airline+"|"+flowKey(f,pdDim)+"|"+(f.season||"");'),
        ('const f0=g[0], flowLabel=flowKey(f0,pdDim);',
         'const f0=g[0], flowLabel=flowKey(f0,pdDim)+(f0.season?(" · "+f0.season):"");'),
    ]
    for old, new in patches:
        if old not in html:
            raise SystemExit(f"patch anchor not found: {old[:50]}")
        html = html.replace(old, new)

    # --- Fix 2: state cells had a fixed height, so long details overflowed into
    # the next row. Let them grow, and keep details on one line.
    css_fix = ("<style>/*fpi-fix*/.st{height:auto!important;min-height:25px;"
               "padding:2px 7px}.st .dt{white-space:nowrap}"
               "table.cond td,table.cond th{vertical-align:middle}</style>")
    html = html.replace("</head>", css_fix + "</head>", 1)

    # --- Fix 3: the Karşılaştırma tab has fixed Economy/Business panels only.
    # Add a Premium Economy panel *only* when the dataset actually has PE fares
    # (every other view derives cabins dynamically and already shows PE).
    if any(f["cabin"] == "Premium Economy" for f in fares):
        pe_patches = [
            ('<div class="cmp-panel"><h4><span class="dot" style="background:var(--biz)"></span>Business</h4><div id="cmpBus"></div></div>',
             '<div class="cmp-panel"><h4><span class="dot" style="background:#7C3AED"></span>Premium Economy</h4><div id="cmpPeco"></div></div>\n        '
             '<div class="cmp-panel"><h4><span class="dot" style="background:var(--biz)"></span>Business</h4><div id="cmpBus"></div></div>'),
            ('[["#cmpEco","Economy"],["#cmpBus","Business"]].forEach',
             '[["#cmpEco","Economy"],["#cmpPeco","Premium Economy"],["#cmpBus","Business"]].forEach'),
        ]
        for old, new in pe_patches:
            if old in html:
                html = html.replace(old, new, 1)

    pat = re.compile(r"(/\*__EMBEDDED_DATA_START__\*/).*?(/\*__EMBEDDED_DATA_END__\*/)", re.DOTALL)
    new_html, n = pat.subn(lambda m: m.group(1) + embedded + m.group(2), html)
    if n != 1:
        raise SystemExit(f"Expected exactly 1 EMBEDDED marker pair, found {n}")
    out_html.write_text(new_html, encoding="utf-8")
    print(f"Injected {len(fares)} fares -> {out_html} ({out_html.stat().st_size//1024} KB)")


if __name__ == "__main__":
    main()
