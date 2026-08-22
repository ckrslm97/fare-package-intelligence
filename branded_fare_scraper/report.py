"""Render an HTML branded-fare comparison, in the style of the reference image.

One card per (carrier, OND, season): brand columns with a tier badge (Eco-1,
PEco-1, Bus-1 …), name and price; amenity rows (the fixed taxonomy) with
✓ / — / paid cells and baggage tooltips ("Included — 8 KG").
"""

from __future__ import annotations

import html
from collections import defaultdict
from pathlib import Path

from .amenities import AMENITY_KEYS, AMENITY_TR
from .models import BrandedFare

CARRIER_NAMES = {
    # Added 2026-08-07 with the OGA-AVRUPA list. This table is not decoration:
    # it is how a results row is matched to the carrier we asked for, so a code
    # missing from here means every row that airline sells is dropped and the
    # unit reports "carrier not on route" — a silent, confident lie about the
    # market. Add the name BEFORE the OND goes into a run, never after.
    "6E": "IndiGo", "AY": "Finnair", "HY": "Uzbekistan Airways",
    "J2": "Azerbaijan Airlines", "LO": "LOT Polish Airlines",
    "PK": "Pakistan International Airlines",
    # Added 2026-08-09 with the LOKAL (Turkey-touching) + domestic lists. Eight
    # of these carry the bulk of the leisure traffic on the Antalya routes, so
    # running without them would have reported the busiest operator on the line
    # as absent: XQ alone is ~293k pax on AYT-DUS, more than TK, PC and XC
    # combined on that pair.
    "XQ": "SunExpress", "XC": "Corendon Airlines", "FH": "Freebird Airlines",
    "LS": "Jet2.com", "2S": "Southwind Airlines", "F3": "flyadeal",
    "TU": "Tunisair", "HH": "Qanot Sharq",
    # Caught by preflight.py on the top50_ond_c list — 9 pairs that would have
    # collected nothing and reported the carriers as absent from their routes.
    "BG": "Biman Bangladesh Airlines", "EO": "Pegas Fly", "EW": "Eurowings",
    "KU": "Kuwait Airways", "SR": "Sundair", "VN": "Vietnam Airlines",
    # The rest of the 300-OND reference list — 70 more pairs, same risk.
    "A2": "Animawings", "BS": "US-Bangla Airlines", "BY": "TUI Airways",
    "D7": "AirAsia X", "DV": "SCAT Airlines", "ET": "Ethiopian Airlines",
    "FB": "Bulgaria Air", "FZ": "flydubai", "G9": "Air Arabia",
    "HO": "Juneyao Airlines", "J9": "Jazeera Airways", "KC": "Air Astana",
    "MF": "XiamenAir", "PR": "Philippine Airlines", "RJ": "Royal Jordanian",
    "RK": "Ryanair UK", "V0": "Conviasa", "WY": "Oman Air",
    "Z0": "Norse Atlantic France", "ZF": "AZUR air",
    "TK": "Turkish Airlines", "EK": "Emirates", "QR": "Qatar Airways", "LH": "Lufthansa",
    "EY": "Etihad Airways", "SV": "Saudia", "PC": "Pegasus", "BA": "British Airways",
    "AF": "Air France", "SU": "Aeroflot", "MS": "EgyptAir", "GF": "Gulf Air",
    "AI": "Air India", "UA": "United Airlines", "KL": "KLM", "A3": "Aegean",
    "VF": "AJet", "KE": "Korean Air", "OZ": "Asiana", "VS": "Virgin Atlantic",
    "MU": "China Eastern", "AZ": "ITA Airways", "AC": "Air Canada", "CX": "Cathay Pacific",
    "SQ": "Singapore Airlines", "MH": "Malaysia Airlines", "QF": "Qantas", "TR": "Scoot",
    "BR": "EVA Air", "CI": "China Airlines", "NH": "ANA", "DL": "Delta", "CA": "Air China",
    "AA": "American Airlines", "JL": "Japan Airlines", "CZ": "China Southern",
    "TG": "Thai Airways", "SK": "SAS", "LX": "SWISS", "OS": "Austrian",
    # Round-15 additions
    "3F": "FlyOne Armenia", "3L": "Air Arabia Abu Dhabi", "A9": "Georgian Airways",
    "DY": "Norwegian", "FR": "Ryanair", "H4": "HiSky", "JX": "Starlux Airlines",
    "T5": "Turkmenistan Airlines", "WZ": "Red Wings",
    # Round-18: carriers that were in the job list but NOT in this table, so
    # every row they published was unrecognisable and their units were written
    # off as "carrier not on route". Measured across the 2026-08-02/03 runs:
    # 140 unit attempts, 0 rows — while the results page was plainly listing
    # them (SKY express, Tarom, Transavia, Hainan Airlines, All Nippon Airways).
    "GQ": "SKY express", "RO": "Tarom", "HV": "Transavia", "HU": "Hainan Airlines",
    "U2": "easyJet", "DE": "Condor", "SN": "Brussels Airlines",
    "UL": "SriLankan Airlines", "3U": "Sichuan Airlines", "OE": "Lauda Europe",
    # Wizz sells the same brand through three AOCs. All three are listed so an
    # exact match decides; without the plain "Wizz Air" entry a page row reading
    # "Wizz Air" is contained in BOTH of the others and resolves to nothing.
    "W6": "Wizz Air", "W4": "Wizz Air Malta", "W9": "Wizz Air UK",
}

#: Alternate spellings an OTA may print for a carrier. Matching only — the
#: PUBLISHED name always comes from ``CARRIER_NAMES`` above, because the study
#: reports the airline's own spelling and never the OTA's label.
CARRIER_ALIASES = {
    "NH": ("All Nippon Airways",),
    "OZ": ("Asiana Airlines",),
    "SK": ("Scandinavian Airlines",),
    "KL": ("KLM Royal Dutch Airlines",),
    "OS": ("Austrian Airlines",),
    "MU": ("China Eastern Airlines", "Shanghai Airlines"),
    "CZ": ("China Southern Airlines",),
    "A3": ("Aegean Airlines",),
    "TG": ("Thai Airways International",),
    "AZ": ("ITA Airways", "Alitalia"),
    "LX": ("Swiss International Air Lines",),
    "SV": ("Saudi Arabian Airlines",),
    "VF": ("AnadoluJet",),
    "UL": ("Sri Lankan Airlines",),
    "DE": ("Condor Flugdienst",),
    # Spellings an OTA prints that the airline itself does not.
    "PK": ("PIA", "Pakistan Intl Airlines", "Pakistan International"),
    "HY": ("Uzbekistan Airways", "Uzbekistan Havo Yullari"),
    "J2": ("AZAL", "Azerbaijan Hava Yollari"),
    "LO": ("LOT", "Polish Airlines LOT"),
    "6E": ("Indigo", "Interglobe Aviation"),
}

AMENITY_ICON = {
    "cabin_baggage": "👜", "checked_baggage": "🧳", "seat_selection": "💺", "meal": "🍽️",
    "lounge_access": "🛋️", "priority_boarding": "🎫", "fast_track": "⚡", "refund": "↩️",
    "change": "🔁", "no_show_refund": "🚫", "no_show_change": "🔀",
    "same_day_earlier_flight": "⏱️", "wifi": "📶", "extra_baggage": "➕", "sports_equipment": "🎿",
    "pet": "🐾",
}

_TIER_CLASS = {"Eco": "eco", "PEco": "peco", "Bus": "bus", "Fir": "fir"}

_CSS = """
:root{--bg:#fff;--fg:#1c2530;--muted:#8a94a3;--line:#e8ecf1;--card:#fff;
--inc:#1a9e5f;--incbg:#e9f7ef;--paid:#c9820b;--paidbg:#fdf3e0;--no:#b7c0cc;--head:#f6f8fb;}
@media (prefers-color-scheme:dark){:root{--bg:#0f141a;--fg:#e6ebf1;--muted:#8a94a3;
--line:#232c37;--card:#151c24;--inc:#3ad98a;--incbg:#12271c;--paid:#e6a94a;
--paidbg:#2a2113;--no:#4a5563;--head:#182029;}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;}
.wrap{max-width:1180px;margin:0 auto;padding:28px 20px 80px;}
h1{font-size:22px;margin:0 0 4px;} .sub{color:var(--muted);margin:0 0 24px;}
.toc{display:flex;flex-wrap:wrap;gap:8px;margin:0 0 28px;}
.toc a{font-size:12px;text-decoration:none;color:var(--fg);background:var(--head);
border:1px solid var(--line);border-radius:999px;padding:4px 11px;}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;
padding:18px 18px 6px;margin:0 0 26px;box-shadow:0 1px 2px rgba(0,0,0,.03);}
.card h2{font-size:16px;margin:0 0 2px;} .card .meta{color:var(--muted);font-size:12px;margin:0 0 14px;}
.scroll{overflow-x:auto;}
table{border-collapse:collapse;width:100%;min-width:520px;}
th,td{padding:9px 10px;text-align:center;border-bottom:1px solid var(--line);}
th.amen,td.amen{text-align:left;white-space:nowrap;color:var(--fg);font-weight:500;min-width:170px;}
td.amen .ic{display:inline-block;width:20px;opacity:.85;}
thead th{vertical-align:bottom;}
.badge{display:inline-block;font-size:10px;font-weight:700;letter-spacing:.3px;color:#fff;
border-radius:999px;padding:2px 9px;margin-bottom:5px;}
.badge.eco{background:#2f6bff}.badge.peco{background:#8b5cf6}.badge.bus{background:#0e9f9f}.badge.fir{background:#111}
.bname{font-weight:600;font-size:13px;} .bprice{color:var(--muted);font-size:11.5px;margin-top:2px;}
.cell-inc{color:var(--inc);font-weight:700;} .cell-inc .d{display:block;font-size:10px;color:var(--muted);font-weight:400;}
.chip-paid{display:inline-block;background:var(--paidbg);color:var(--paid);font-weight:700;
font-size:11px;border-radius:6px;padding:2px 7px;} .cell-no{color:var(--no);font-weight:700;}
.cell-unk{color:var(--no);} tbody tr:hover{background:rgba(127,127,127,.04);}
.foot{color:var(--muted);font-size:11px;margin-top:26px;text-align:center;}
"""


def _carrier_title(code: str) -> str:
    name = CARRIER_NAMES.get(code.upper())
    return f"{name} ({code.upper()})" if name else code.upper()


def _price(bf: BrandedFare) -> str:
    if bf.calculated_absolute_price is None:
        return html.escape(bf.display_price or "")
    cur = bf.currency or ""
    return f"{bf.calculated_absolute_price:,.2f} {cur}".strip()


def _cell(bf: BrandedFare, key: str) -> str:
    status = bf.amenities.get(key, "Unknown")
    detail = bf.amenity_details.get(key, "")
    if status == "Included":
        d = f'<span class="d">{html.escape(detail)}</span>' if detail else ""
        title = f"Included — {detail}" if detail else "Included"
        return f'<span class="cell-inc" title="{html.escape(title)}">✓{d}</span>'
    if status == "Paid":
        label = html.escape(detail) if detail else "Paid"
        return f'<span class="chip-paid" title="Paid: {html.escape(detail or "for a fee")}">{label}</span>'
    if status == "Not Included":
        return '<span class="cell-no" title="Not included">—</span>'
    return '<span class="cell-unk" title="Unknown">·</span>'


def _card(group_key: tuple, brands: list[BrandedFare]) -> str:
    carrier, origin, dest, season = group_key
    brands = sorted(brands, key=lambda b: (0 if b.tier_code.startswith("Eco") else
                                           1 if b.tier_code.startswith("PEco") else
                                           2 if b.tier_code.startswith("Bus") else 3,
                                           b.brand_order))
    dep = brands[0].departure_date.isoformat() if brands[0].departure_date else "?"
    ret = brands[0].return_date.isoformat() if brands[0].return_date else "?"
    src = brands[0].source

    heads = ['<th class="amen"></th>']
    for b in brands:
        tclass = _TIER_CLASS.get(b.tier_code.split("-")[0], "eco")
        heads.append(
            f'<th><span class="badge {tclass}">{html.escape(b.tier_code)}</span><br>'
            f'<span class="bname">{html.escape(b.raw_brand_name)}</span>'
            f'<div class="bprice">{html.escape(_price(b))}</div>'
            f'<div class="bprice">{html.escape(b.cabin.value)}</div></th>')

    body = []
    for key in AMENITY_KEYS:
        cells = "".join(f"<td>{_cell(b, key)}</td>" for b in brands)
        body.append(
            f'<tr><td class="amen"><span class="ic">{AMENITY_ICON.get(key,"•")}</span>'
            f'{html.escape(AMENITY_TR[key])}</td>{cells}</tr>')
    # miles row
    mrow = "".join(
        f'<td>{("✓" if b.mileage_available else "—") if b.mileage_available is not None else "·"}'
        f'{(" " + str(int(b.miles_earned))) if b.miles_earned else ""}</td>' for b in brands)
    body.append(f'<tr><td class="amen"><span class="ic">🏅</span>Mil Kazanımı</td>{mrow}</tr>')

    anchor = f"{carrier}-{origin}-{dest}-{season}".lower()
    return (
        f'<section class="card" id="{html.escape(anchor)}">'
        f'<h2>{html.escape(_carrier_title(carrier))} &nbsp;·&nbsp; {html.escape(origin)}–{html.escape(dest)}</h2>'
        f'<p class="meta">{html.escape(season)} · dep {dep} → ret {ret} · source: {html.escape(src)}</p>'
        f'<div class="scroll"><table><thead><tr>{"".join(heads)}</tr></thead>'
        f'<tbody>{"".join(body)}</tbody></table></div></section>')


def build_report_html(rows: list[BrandedFare], title: str = "Branded Fare Comparison") -> str:
    groups: dict[tuple, list[BrandedFare]] = defaultdict(list)
    for r in rows:
        groups[(r.carrier, r.origin, r.destination, r.season.value)].append(r)

    ordered = sorted(groups.items(), key=lambda kv: (kv[0][1], kv[0][2], kv[0][0], kv[0][3]))
    toc = "".join(
        f'<a href="#{html.escape(f"{c}-{o}-{d}-{s}".lower())}">{html.escape(o)}–{html.escape(d)} {html.escape(c)}</a>'
        for (c, o, d, s), _ in ordered)
    cards = "".join(_card(k, v) for k, v in ordered)
    return (
        f'<!doctype html><html lang="tr"><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>{html.escape(title)}</title><style>{_CSS}</style></head><body><div class="wrap">'
        f'<h1>{html.escape(title)}</h1>'
        f'<p class="sub">{len(rows)} branded fares · {len(ordered)} carrier×OND comparisons</p>'
        f'<div class="toc">{toc}</div>{cards}'
        f'<p class="foot">Generated by branded_fare_scraper · statuses: ✓ Included · '
        f'Paid (fee) · — Not Included · · Unknown</p>'
        f'</div></body></html>')


def write_report(rows: list[BrandedFare], out_dir: Path, title: str = "Branded Fare Comparison") -> Path:
    path = out_dir / "report.html"
    path.write_text(build_report_html(rows, title), encoding="utf-8")
    return path
