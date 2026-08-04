#!/usr/bin/env python3
"""Build the scrape input for everything AFTER the first 150 rows already done
(see build_150_input.py) — rows 151..712 of the reference list.

    python build_rest_input.py
"""
from __future__ import annotations
import csv
from pathlib import Path
import openpyxl

SRC = Path("/Users/selim/Downloads/FPI_REF_OND_CRR_V2.csv")
OUT = Path("run_150/input_rest.xlsx")
SKIP = 150


def main() -> None:
    rows = list(csv.DictReader(SRC.open(encoding="utf-8-sig")))
    rest = rows[SKIP:]

    bad_codes = [r for r in rest
                 if not (len(r["ORIGIN"]) == 3 and r["ORIGIN"].isalpha())
                 or not (len(r["DEST"]) == 3 and r["DEST"].isalpha())
                 or not r["CARRIER"].strip()]
    if bad_codes:
        raise SystemExit(f"{len(bad_codes)} malformed row(s): {bad_codes[:5]}")
    keys = [(r["ORIGIN"], r["DEST"], r["CARRIER"]) for r in rest]
    if len(keys) != len(set(keys)):
        dupes = {k for k in keys if keys.count(k) > 1}
        raise SystemExit(f"duplicate (origin,dest,carrier): {dupes}")
    # No overlap with the first-150 batch already scraped.
    first150 = {(r["ORIGIN"], r["DEST"], r["CARRIER"]) for r in rows[:SKIP]}
    overlap = set(keys) & first150
    if overlap:
        raise SystemExit(f"overlap with already-scraped first 150: {overlap}")

    OUT.parent.mkdir(exist_ok=True)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["OND", "CARRIER"])
    for r in rest:
        ws.append([f"{r['ORIGIN']}-{r['DEST']}", r["CARRIER"]])
    wb.save(OUT)

    onds = {(r["ORIGIN"], r["DEST"]) for r in rest}
    carriers = {r["CARRIER"] for r in rest}
    print(f"{OUT}: {len(rest)} row(s), {len(onds)} distinct OND(s), "
          f"{len(carriers)} distinct carrier(s)")


if __name__ == "__main__":
    main()
