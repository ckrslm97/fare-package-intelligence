#!/usr/bin/env python3
"""Build ONE input covering every reference row: the 712-row international
list plus the 66-row domestic/local list, deduped.

The two source files disagree on column naming (DEST vs DESTINATION) and the
local one carries a PAX column the other lacks, so both are normalized here
rather than at read time.

    python build_full_input.py
"""
from __future__ import annotations
import csv
from pathlib import Path
import openpyxl

SOURCES = [
    (Path("/Users/selim/Downloads/FPI_REF_OND_CRR_V2.csv"), "DEST"),
    (Path("/Users/selim/Downloads/FPI_REF_OND_CRR_LOKAL.csv"), "DESTINATION"),
]
OUT = Path("run_full/input_full.xlsx")


def main() -> None:
    seen: set[tuple[str, str, str]] = set()
    rows: list[tuple[str, str]] = []
    per_file = []
    for path, dest_col in SOURCES:
        n_new = n_dupe = 0
        with path.open(encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                o = (r.get("ORIGIN") or "").strip().upper()
                d = (r.get(dest_col) or "").strip().upper()
                c = (r.get("CARRIER") or "").strip().upper()
                if not (len(o) == 3 and o.isalpha() and len(d) == 3 and d.isalpha() and c):
                    raise SystemExit(f"{path.name}: malformed row {r}")
                key = (o, d, c)
                if key in seen:
                    n_dupe += 1
                    continue
                seen.add(key)
                rows.append((f"{o}-{d}", c))
                n_new += 1
        per_file.append((path.name, n_new, n_dupe))

    OUT.parent.mkdir(exist_ok=True)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["OND", "CARRIER"])
    for ond, carrier in rows:
        ws.append([ond, carrier])
    wb.save(OUT)

    for name, n_new, n_dupe in per_file:
        print(f"  {name}: +{n_new} yeni" + (f", {n_dupe} tekrar atlandı" if n_dupe else ""))
    onds = {r[0] for r in rows}
    carriers = {r[1] for r in rows}
    print(f"{OUT}: {len(rows)} satır, {len(onds)} OND, {len(carriers)} taşıyıcı")


if __name__ == "__main__":
    main()
