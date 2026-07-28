"""Generate a sample input workbook.

Usage:
    python make_sample_input.py [output.xlsx]

Columns: Carrier, Origin, Destination — one row per scraping task.
Replace these rows with your real (Carrier, Origin, Destination) list.
"""

from __future__ import annotations

import sys

SAMPLE_ROWS = [
    ("TK", "IST", "LHR"),
    ("TK", "IST", "JFK"),
    ("TK", "IST", "CDG"),
]


def main() -> int:
    out = sys.argv[1] if len(sys.argv) > 1 else "sample_input.xlsx"
    if out.lower().endswith(".csv"):
        import csv
        with open(out, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["Carrier", "Origin", "Destination"])
            w.writerows(SAMPLE_ROWS)
    else:
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.title = "Jobs"
        ws.append(["Carrier", "Origin", "Destination"])
        for r in SAMPLE_ROWS:
            ws.append(list(r))
        wb.save(out)
    print(f"Wrote {out} with {len(SAMPLE_ROWS)} rows.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
