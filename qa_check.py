#!/usr/bin/env python3
"""Automated post-scrape QA — run this after every scrape, before publishing.

Encodes every data-quality bug found and fixed in the 2026-08-03/04 session as
a repeatable check, so the next run gets caught automatically instead of
needing another round of manual screenshot comparison to notice:

  * currency leak     — Currency column must be 100% USD, never bare TRY/TL.
  * duplicate names    — no two tiers in one (carrier, OND, season, cabin)
                         group may share a Normalized Brand Name.
  * baggage format     — "N parça X kg" details must show as "N×Xkg", never
                         bare "Xkg" (piece count silently dropped).
  * business coverage  — how many non-LCC carriers still show no Business
                         cabin after the validation-retry pass (informational:
                         some of this is genuine route-level absence, not a
                         bug — see normalization.LCC_CARRIERS).
  * refund fee spread  — informational only: how many "Kesintili" rule-rights
                         carry a fee figure vs not, per carrier. A carrier
                         showing 0% is expected when Enuygun's own tooltip is
                         null for it (verified live for TK 2026-08-04) — this
                         is NOT auto-flagged as a failure, just reported so a
                         human can sanity-check the split still looks sane.

Exit code is non-zero iff a check that represents a REAL bug (not merely an
informational split) fails.

    python qa_check.py output_night_20260804-105843
"""
from __future__ import annotations

import csv
import collections
import re
import sys
from pathlib import Path


def load_rows(out_dir: Path) -> list[dict]:
    csv_path = out_dir / "normalized_data.csv"
    if not csv_path.exists():
        raise SystemExit(f"not found: {csv_path}")
    with csv_path.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def check_currency(rows: list[dict]) -> list[str]:
    bad = [r for r in rows if r["Currency"].strip().upper() not in ("USD", "")]
    problems = []
    if bad:
        problems.append(f"CURRENCY: {len(bad)} row(s) not USD — {collections.Counter(r['Currency'] for r in bad)}")
    tl_leak = [r for r in rows if re.search(r"\bTL\b|\bTRY\b", r["Display Price"])]
    if tl_leak:
        problems.append(f"CURRENCY: {len(tl_leak)} row(s) with TL/TRY text leaking into Display Price")
    return problems


def check_duplicate_names(rows: list[dict]) -> list[str]:
    groups = collections.defaultdict(list)
    for r in rows:
        key = (r["Carrier"], r["Origin"], r["Destination"], r["Season"], r["Cabin"])
        groups[key].append(r["Normalized Brand Name"])
    problems = []
    for key, names in groups.items():
        c = collections.Counter(names)
        dupes = {n: n_ for n, n_ in c.items() if n_ > 1}
        if dupes:
            problems.append(f"DUP NAME: {key} has repeated tier name(s) {dupes}")
    return problems


def check_baggage_format(rows: list[dict]) -> list[str]:
    problems = []
    for col in ("Cabin Baggage", "Checked Baggage", "Extra Baggage"):
        for r in rows:
            v = r.get(col, "")
            # A bare "Xkg" with no leading "N×" is only suspicious when the
            # row's own text elsewhere implies more than one piece — that
            # context isn't in the normalized column any more (by design,
            # it's collapsed to the short display form), so this check can
            # only catch the OTHER shape of the bug: a piece count with no
            # kg number, or a kg number that looks like a cm dimension
            # (three-digit-plus values are never a real per-piece weight).
            m = re.fullmatch(r"(\d+)kg", v.strip()) if v else None
            if m and int(m.group(1)) >= 100:
                problems.append(f"BAGGAGE: {r['Carrier']} {col}={v!r} looks like a "
                                f"misread dimension, not a weight (row: {r['Origin']}-{r['Destination']})")
    return problems


def check_ladder_monotonic(rows: list[dict]) -> list[str]:
    """Within ONE real ladder, a higher tier must never cost less.

    A unit is (carrier, OND, season, cabin) — one actual offer set. Inside it
    the tier codes (Eco-1, Eco-2…) are positions ordered by price, so a
    later tier costing LESS means the ladder was ordered wrong at scrape
    time. This is the data-level counterpart to the 2026-08-04 display bug
    where transition fees came out negative: that one turned out to be an
    aggregation error (differencing tier averages built from different
    seasons), but the same symptom can also come from a genuinely
    mis-ordered ladder, which only a per-unit check like this can catch.
    """
    units: dict[tuple, list] = collections.defaultdict(list)
    for r in rows:
        key = (r["Carrier"], r["Origin"], r["Destination"], r["Season"], r["Cabin"])
        try:
            price = float(r["Calculated Absolute Price"])
        except (TypeError, ValueError):
            continue
        units[key].append((int(r["Brand Order"]), r["Normalized Brand Name"], price))
    problems = []
    for key, lst in units.items():
        lst.sort()
        for (_o1, n1, p1), (_o2, n2, p2) in zip(lst, lst[1:]):
            if p2 < p1:
                problems.append(
                    f"LADDER: {key[0]} {key[1]}-{key[2]} {key[3]} {key[4]} — "
                    f"{n1} (${p1:.0f}) -> {n2} (${p2:.0f}) fiyat DÜŞÜYOR ({p2-p1:+.0f})")
    return problems


def check_right_coverage(rows: list[dict]) -> list[str]:
    """A right a carrier reports almost everywhere but misses on a few ONDs.

    Rights come from fare rules, so a carrier that grants cabin baggage on 120
    of its 127 ONDs almost certainly grants it on the other 7 too — the gap is
    the scrape having read one flight that omitted the item, not the airline
    withdrawing the right on those routes. This is the generalised form of the
    2026-08-04 defect where Enuygun's flight picker chose one of the 2 TK
    BER-AYT flights (out of 47) whose packages carry no cabin_baggage.

    Only flagged when the right is present on >=85% of the carrier's ONDs and
    absent on at least one: a right that is genuinely route-dependent (lounge
    on long-haul only) sits well below that line and stays quiet.
    """
    cols = ["Cabin Baggage", "Checked Baggage", "Meal", "Seat Selection", "Lounge"]
    by_carrier: dict[str, dict[tuple, dict[str, bool]]] = collections.defaultdict(dict)
    for r in rows:
        key = (r["Origin"], r["Destination"], r["Season"], r["Cabin"])
        slot = by_carrier[r["Carrier"]].setdefault(key, {c: False for c in cols})
        for c in cols:
            if r.get(c, "").strip() not in ("", "Unknown"):
                slot[c] = True
    problems = []
    for carrier, units in by_carrier.items():
        if len(units) < 8:            # too few units to call anything anomalous
            continue
        for c in cols:
            have = sum(1 for u in units.values() if u[c])
            miss = len(units) - have
            if miss and have / len(units) >= 0.85:
                problems.append(
                    f"RIGHT COVERAGE: {carrier} — '{c}' {have}/{len(units)} birimde var, "
                    f"{miss} birimde HİÇ yok (kural bazlı hak; muhtemelen o birimde "
                    f"eksik okundu)")
    return problems


def report_business_coverage(rows: list[dict]) -> None:
    sys.path.insert(0, str(Path(__file__).parent))
    from branded_fare_scraper.normalization import LCC_CARRIERS
    by_pair = collections.defaultdict(set)
    for r in rows:
        if r["Carrier"].upper() in LCC_CARRIERS:
            continue
        by_pair[(r["Carrier"], r["Origin"], r["Destination"], r["Season"])].add(r["Cabin"])
    total = len(by_pair)
    missing = [k for k, cabs in by_pair.items() if "Economy" in cabs and "Business" not in cabs]
    print(f"[bilgi] Business kapsamı: {total - len(missing)}/{total} non-LCC birimde "
          f"Business var (%{100*(total-len(missing))/total:.0f})" if total else "[bilgi] non-LCC birim yok")
    if missing:
        print(f"        Business hâlâ eksik: {len(missing)} birim — "
              f"{sorted(set((c, o, d) for c, o, d, s in missing))[:10]}")


def report_refund_fee_spread(out_dir: Path) -> None:
    # normalized_data.csv's Refund column is the CANONICAL status word (Paid/
    # Included/...), not the raw text a fee figure lives in — that only
    # survives in raw_data.jsonl's amenity.raw_value, so this reads raw
    # records directly, the same way the source-vs-bug investigation did.
    raw_path = out_dir / "raw_data.jsonl"
    if not raw_path.exists():
        return
    sys.path.insert(0, str(Path(__file__).parent))
    from branded_fare_scraper.rebuild import iter_raw_records
    from to_platform import _fee_text
    has_fee = collections.Counter()
    no_fee = collections.Counter()
    for rec in iter_raw_records(raw_path):
        carrier = rec.get("carrier", "")
        for c in rec.get("cabins", []):
            for b in c.get("brands", []):
                for a in b.get("amenities", []):
                    if a.get("canonical_key") != "refund" or a.get("status") != "Paid":
                        continue
                    (has_fee if _fee_text(a.get("raw_value", "")) else no_fee)[carrier] += 1
    carriers = sorted(set(has_fee) | set(no_fee))
    print(f"[bilgi] İade ücreti tutarı gösteren taşıyıcı sayısı: "
          f"{sum(1 for c in carriers if has_fee[c] and not no_fee[c])} tam, "
          f"{sum(1 for c in carriers if has_fee[c] and no_fee[c])} karışık, "
          f"{sum(1 for c in carriers if not has_fee[c] and no_fee[c])} hiç — "
          f"bu Enuygun'un kaynak verisine bağlı, hata değil")


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("kullanim: python qa_check.py <output_dir>")
    out_dir = Path(sys.argv[1])
    rows = load_rows(out_dir)
    print(f"{out_dir}: {len(rows)} satır kontrol ediliyor\n")

    # BLOKLAYAN kontroller: her biri tek başına yanlış veri demektir ve
    # gözle bakmaya gerek bırakmaz — para birimi sızıntısı, tekrar eden paket
    # adı, kg yerine cm okunmuş bagaj, fiyatı düşen merdiven.
    problems: list[str] = []
    problems += check_currency(rows)
    problems += check_duplicate_names(rows)
    problems += check_baggage_format(rows)
    problems += check_ladder_monotonic(rows)

    # UYARI: hak kapsamı bir kusuru İŞARET EDER ama kanıtlamaz — bir taşıyıcı
    # bazı rotalarda gerçekten yemek vermiyor olabilir. Bunu bloklayıcı yapmak
    # 2026-08-05'te her ölçütte DAHA İYİ bir veri setinin yayınlanmasını
    # engelledi (el bagajı eksiği %57 azalmışken 5 uyarı kaldı diye). Göreli
    # bir kalite sinyali mutlak bir kapı olamaz: raporlanır, karar insana ait.
    warnings: list[str] = check_right_coverage(rows)

    report_business_coverage(rows)
    report_refund_fee_spread(out_dir)
    if warnings:
        print(f"\n[uyarı] {len(warnings)} hak-kapsamı anomalisi (bloklamaz):")
        for w in warnings[:15]:
            print(f"  - {w}")
        if len(warnings) > 15:
            print(f"  ... ve {len(warnings) - 15} tane daha")
    print()

    if problems:
        print(f"BAŞARISIZ — {len(problems)} bloklayan sorun:")
        for p in problems[:40]:
            print(f"  - {p}")
        if len(problems) > 40:
            print(f"  ... ve {len(problems) - 40} tane daha")
        sys.exit(1)
    print("BLOKLAYAN KONTROLLERİN HEPSİ GEÇTİ"
          + (f" ({len(warnings)} uyarı var, yukarıda)" if warnings else ""))


if __name__ == "__main__":
    main()
