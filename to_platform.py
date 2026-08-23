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
from branded_fare_scraper.amenities import canonical_rule_detail
from branded_fare_scraper.models import AmenityStatus
from branded_fare_scraper.normalization import (LCC_CARRIERS, brand_match_key,
                                                clean_fare_code,
                                                iter_unit_ranked_by_cabin, tier_code)
from branded_fare_scraper.publish import validation_lookup, window_diffs_for
from branded_fare_scraper.rebuild import (cabin_result_from_dict, iter_raw_records,
                                          pair_prefs_for, season_pair_prefs)
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
    # Added 2026-08-06 with the taxonomy. Platform keys match the canonical
    # ones so there is nothing to remember; `prune_empty_features` still drops
    # any of these that no carrier in the dataset actually reports, so adding
    # them cannot put an empty row on every card.
    "personal_item": "personal_item", "priority_baggage": "priority_baggage",
    "extra_legroom": "extra_legroom", "priority_check_in": "priority_check_in",
    "airport_check_in": "airport_check_in", "entertainment": "entertainment",
    "upgrade_eligibility": "upgrade_eligibility",
    "infant_benefits": "infant_benefits", "tier_miles": "tier_miles",
}


#: "Yakl. 3531.00 TRY (48 saate kadar)" -> 3531.00 + TRY. Enuygun writes the
#: figure into the tooltip; Trip.com's phrasing ("Change fee: from $180") puts
#: the symbol first, so both orders are accepted.
_FEE_RE = re.compile(
    r"(?:([$€£])\s*([\d][\d.,]*)|([\d][\d.,]*)\s*(TRY|USD|EUR|GBP|TL))", re.I)
_SYMBOL = {"$": "USD", "€": "EUR", "£": "GBP"}
#: The platform reports every price in USD (see BrandedFare.currency, which
#: adapters always normalize) EXCEPT this one tooltip string, which is the
#: raw text an OTA's own rule-fee popup shows and was never run through that
#: conversion. No per-record rate survives to this stage (Enuygun's own
#: currency_rates are read and applied at scrape time, then discarded), so a
#: fixed approximate rate is used here — acceptable because the figure is
#: already presented as an estimate ("~"), not a bookable price.
_TO_USD = {"USD": 1.0, "TRY": 1 / 41.0, "EUR": 1.09, "GBP": 1.27}


def _fee_text(raw: str) -> str:
    """"~86 USD" for a rule right that states its price, else "". Always USD —
    every other currency is converted (see _TO_USD)."""
    m = _FEE_RE.search(raw or "")
    if not m:
        return ""
    sym, after, before, cur = m.groups()
    amount = after or before or ""
    currency = _SYMBOL.get(sym or "", "") or (cur or "").upper()
    if currency == "TL":
        currency = "TRY"
    try:                      # "3531.00" / "3.531,00" -> 3531
        n = float(amount.replace(".", "").replace(",", ".")
                  if amount.count(",") == 1 and amount.rfind(",") > amount.rfind(".")
                  else amount.replace(",", ""))
    except ValueError:
        return ""
    n *= _TO_USD.get(currency, 1.0)
    currency = "USD"
    if n <= 0:
        return ""
    return f"~{n:,.0f} {currency}".replace(",", ".")


def _short_detail(key: str, raw: str, status: AmenityStatus) -> str:
    """Concise one-line detail so cells don't overflow the row.

    Rule rights use ONE standardized Turkish vocabulary (Kesintili/Kesintisiz,
    Ücretli/Cezasız…) instead of each source's phrasing; baggage stays numeric.
    """
    canon = canonical_rule_detail(key, status)
    if canon is not None:
        # The standardized word stays FIRST and unchanged — the vocabulary rule
        # is about how a right is named, not about hiding what it costs. Enuygun
        # publishes the actual figure ("Yakl. 3531.00 TRY (48 saate kadar)") and
        # returning the word alone threw it away on 340 of 624 rule rights.
        fee = _fee_text(raw)
        return f"{canon} · {fee}" if fee else canon
    raw = (raw or "").strip()
    if not raw:
        return ""
    if key in ("cabin_baggage", "checked_baggage", "extra_baggage"):
        # "1 parça X 8 kg kabin bagajı (55x40x20 cm)": the OLD pattern had no
        # "kg" anchor and no case-insensitive flag, so it missed "X" (capital)
        # here and fell through to the dimensions in parentheses instead,
        # reading "55x40" as pieces×kg — live-verified 2026-08-04 on Condor.
        # Requiring "kg" right after the second number makes that impossible:
        # a WxHxD-in-cm triple is never followed by "kg", so it can never match.
        m2 = re.search(r"(\d+)\s*(?:parça|pieces?|pc)?\s*[x×]\s*(\d+)\s*kg", raw, re.I)
        if m2:
            return f"{m2.group(1)}×{m2.group(2)}kg"
        m1 = re.search(r"(\d+)\s*kg", raw, re.I)             # weight only
        if m1:
            # A source can state the piece count and the weight WITHOUT an
            # "x"/"×" between them ("2 parça, 8 kg"). Dropping the piece count
            # here would be the exact "8kg instead of 2×8kg" complaint — check
            # for it separately rather than only ever showing bare weight.
            mp = re.search(r"(\d+)\s*(?:parça|pieces?|pc)\b", raw, re.I)
            if mp:
                return f"{mp.group(1)}×{m1.group(1)}kg"
            return f"{m1.group(1)}kg"
        return ""                                            # e.g. "1 piece" -> just ✓
    return ""  # other rights: the ✓/€/— state colour already carries the meaning


def _lead_days(dep: str | None) -> int | None:
    """Days from today to ``dep`` (ISO date), or None when unknown.

    Deliberately computed at publish time from the departure rather than
    stored during collection: the raw files predate the field, and a run's own
    clock is the only honest reference for "how far ahead was this bought".
    Rows published in different runs therefore carry leads measured from their
    own publish date, which is what a comparison inside one panel needs.
    """
    if not dep:
        return None
    try:
        return (date.fromisoformat(dep) - date.today()).days
    except ValueError:
        return None


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
        det = _short_detail(a.canonical_key or "", a.raw_value, a.status)
        if det:
            feat["detail"] = det
        # Provenance travels with the cell. `detail` is the tidied display
        # string; `src` is what the page actually said, which is the only one
        # of the two that proves anything.
        if getattr(a, "source_text", ""):
            feat["src"] = a.source_text[:180]
        if getattr(a, "confidence", 0):
            feat["conf"] = int(a.confidence)
        out[pk] = feat
    # miles feature from mileage flag (earned count, else the bonus percentage)
    if raw.miles.mileage_available is not None:
        out["miles"] = {"state": "Included" if raw.miles.mileage_available else "Not Included"}
        if raw.miles.miles_earned:
            out["miles"]["detail"] = f"{int(raw.miles.miles_earned)} mil"
        elif raw.miles.bonus_percent:
            out["miles"]["detail"] = f"%{int(raw.miles.bonus_percent)} bonus"
    return out


def used_feature_keys(fares) -> list[str]:
    """Platform feature keys that at least one fare in the dataset carries.

    User rule: "hiçbir taşıyıcıda olmayan paket içeriklerini kaldır" — a right
    no carrier in the dataset reports is an empty row on every card, so it is
    dropped from the display. The raw data keeps the full taxonomy.
    """
    used: set[str] = set()
    for f in fares:
        for k, v in (f.get("features") or {}).items():
            if v and v.get("state"):
                used.add(k)
    return sorted(used)


def prune_empty_features(fares) -> list[str]:
    """Drop stateless feature entries in place; return the surviving keys."""
    keep = set(used_feature_keys(fares))
    for f in fares:
        feats = f.get("features") or {}
        for k in [k for k in feats if k not in keep]:
            del feats[k]
    return sorted(keep)


#: Anchor for the template's FEATURE_META table (last row + closing brace).
FEATURE_META_ANCHOR = ('  miles:            ["Mil Kazanımı",'
                       '"Sadakat programı mil kazanım oranı"],\n};')


def feature_filter_patch(keys) -> tuple[str, str]:
    """(anchor, replacement) that hides FEATURE_META rows absent from the data.

    Every view enumerates rights with ``Object.keys(FEATURE_META)``, so pruning
    the table itself removes the empty rows/columns everywhere at once
    (matrix, cards, TSV/XLS export) without touching each view.
    """
    allowed = json.dumps(sorted(keys), ensure_ascii=False)
    return (FEATURE_META_ANCHOR,
            FEATURE_META_ANCHOR + "\n"
            "/*fpi-fix: rights with no data in this dataset are not rendered*/\n"
            "/* …in the ANALYSIS views. The manual editor keeps the full table:\n"
            "   pruning asks \"did the scrape find this right?\", and the editor\n"
            "   exists precisely for the rights it did not. Handing it the\n"
            "   pruned list would make the tool unable to record anything the\n"
            "   source never publishes — which is most of what a human has to\n"
            "   add. Snapshot BEFORE the deletion, hence the order here. */\n"
            "const FEATURE_META_ALL = Object.assign({}, FEATURE_META);\n"
            f"const FPI_PRESENT_FEATURES = new Set({allowed});\n"
            "Object.keys(FEATURE_META).forEach(k=>{"
            "if(!FPI_PRESENT_FEATURES.has(k)) delete FEATURE_META[k];});")


def build_fares(out_dir: Path):
    now = datetime.now().replace(microsecond=0).isoformat()
    coll_month = datetime.now().strftime("%Y-%m")
    fares = []
    raw_path = out_dir / "raw_data.jsonl"
    prefs = season_pair_prefs(raw_path)      # cross-season ladder-order consensus
    for rec in iter_raw_records(raw_path):
        carrier = rec["carrier"]; o = rec["origin"]; d = rec["destination"]
        season = rec["season"]
        ppc = pair_prefs_for(prefs, carrier, o, d)
        mo = airport_meta(o); md = airport_meta(d)
        oc, oreg = mo["country_name"], mo["region"]
        dc, dreg = md["country_name"], md["region"]
        ctype = "Low Cost" if carrier.upper() in LCC_CARRIERS else "Legacy"
        # YIC (yurt ici) if BOTH endpoints are Turkey, Local if only one is,
        # else Beyond. Derived from the airport table rather than tagged by
        # hand on the input sheet, so a domestic OND added later is classified
        # correctly without anyone remembering to label it.
        _tr = [mo["country_code"] == "TR", md["country_code"] == "TR"]
        ondtype = "YIC" if all(_tr) else ("Local" if any(_tr) else "Beyond")
        cabins = [cabin_result_from_dict(c) for c in rec.get("cabins", [])]
        vblock = rec.get("validation")
        vindex = validation_lookup(vblock)
        # One ladder per effective cabin for the unit (no duplicate PE rows).
        for eff_cab, raw, nb, order, absp, c in iter_unit_ranked_by_cabin(
                cabins, carrier=carrier, pair_prefs_by_cabin=ppc):
            ventry = vindex.get((eff_cab.value, brand_match_key(raw.raw_brand_name)), {})
            dep = c.departure.isoformat() if c.departure else None
            src = c.source or rec.get("source", "")   # per-cabin source from raw
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
                "brand_code": clean_fare_code(raw.fare_family_code) or nb.subtier or "",
                "std_tier": tier_code(eff_cab, order), "package_order": order + 1,
                "price": price, "price_usd": price_usd,
                "currency": cur, "travel_date": dep,
                # Days between collection and departure. Two rows in the same
                # OND+season bucket are only comparable if this is close: a
                # fare read 43 days out is structurally dearer than the same
                # product read 300 days out. It was already implicit in
                # travel_date, but nothing surfaced it, so a deep-band legacy
                # price and a near-band low-cost price sat side by side looking
                # like a like-for-like comparison. Measured 2026-08-09: 52% of
                # OND+season groups mix departure dates, and low-cost carriers
                # are the ones that fall back, because they do not sell as far
                # ahead as legacy carriers do.
                "lead_days": _lead_days(dep),
                "features": _features(raw),
                # Identity of the itinerary the ladder was read from. Was hard
                # -coded None because no source recorded it; Enuygun does, and
                # it is what lets a price be traced back to a real departure.
                "source": src, "flight_no": c.flight_no or None,
                "booking_class": c.booking_class or None,
                "operating_carrier": c.operating_carrier or None,
                "is_codeshare": c.is_codeshare,
                "is_interline": c.is_interline,
                # --- validation layer. Present so a reader can weigh a number
                # instead of only reading it; absent keys mean the run that
                # produced this data predates the layer, not that it passed.
                "window_role": c.window_role,
                "validation": ventry.get("status") or None,
                "observations": ventry.get("observations") or None,
                "confidence": ventry.get("mean_confidence") or None,
                "contested": ventry.get("contested") or None,
                "product_change": window_diffs_for(
                    vblock, eff_cab.value, raw.raw_brand_name) or None,
            })
    return fares


def build_runs(out_dir: Path, fares: list[dict]) -> list[dict]:
    """Archive rows for the platform's Arşiv tab, one per collection season.

    The tab was rendering "no archived collection yet" because nothing ever filled
    `runs`. The facts come from summary.json (run id, timings) plus the fares
    themselves (which carriers and ONDs that season actually produced).
    """
    try:
        summary = json.loads((out_dir / "summary.json").read_text())
    except (OSError, ValueError):
        return []
    # Group by SEASON, not by coll_date: one calendar run collects both the
    # summer and the winter window, so every fare shares the same coll_date and
    # grouping on it would collapse two distinct collections into one row.
    by_season: dict[tuple, list[dict]] = {}
    for f in fares:
        by_season.setdefault((f["coll_date"], f.get("coll_season", "")), []).append(f)
    runs = []
    for (coll_date, season), rows in sorted(by_season.items()):
        runs.append({
            "run_id": f"{summary.get('run_id', 'run')}_{season or coll_date}",
            "coll_date": coll_date,
            "coll_season": rows[0].get("coll_season", ""),
            "query_date": rows[0].get("query_date", ""),
            "collected_at": summary.get("finished_at") or summary.get("started_at", ""),
            "airlines": sorted({r["airline"] for r in rows}),
            "onds": sorted({f"{r['origin']}-{r['destination']}" for r in rows}),
            "record_count": len(rows),
            "status": "OK",
            "ond_type": "Karma",
        })
    return runs


def main():
    template = Path(sys.argv[1])
    out_dir = Path(sys.argv[2])
    out_html = Path(sys.argv[3])
    fares = build_fares(out_dir)
    present = prune_empty_features(fares)     # rights no carrier reports at all
    payload = {"generated_at": datetime.now().replace(microsecond=0).isoformat(),
               "count": len(fares), "fares": fares,
               "runs": build_runs(out_dir, fares)}
    embedded = json.dumps(payload, ensure_ascii=False)
    html = template.read_text(encoding="utf-8")

    # --- Fix 1: the detail view grouped by carrier|OND only, so both travel
    # seasons became duplicate columns. Split cards by season and label them.
    # Yayın anında string değiştirerek şablona yama atmak KIRILGAN: şablondaki
    # o satır bir kez düzenlendiğinde tutamaç kaçar ve yayın "patch anchor not
    # found" ile düşer (2026-08-23'te bilfiil oldu). Sezona göre gruplama ve
    # kart etiketi bu yüzden ŞABLONUN İÇİNE alındı; burada yalnızca veriye
    # bağlı olan, yani şablona sabitlenmesi mümkün olmayan yama kaldı.
    patches = [
        feature_filter_patch(present),
        # NOTE: a patch used to sit here that hid negative / same-brand-on-both-
        # sides transitions from the KPI strip. Its own comment named the cause
        # correctly ("chainsFor averages tiers across ONDs, so mixed ladder
        # lengths can make a tier-mean step negative") but only masked it in
        # that one widget — Paket Karşılaştırma still published "+$-366
        # Flexible → Restricted". chainsFor now averages PER-UNIT deltas, so
        # the condition can no longer arise and the KPI strip itself is gone.
    ]
    for old, new in patches:
        if old not in html:
            raise SystemExit(f"patch anchor not found: {old[:50]}")
        html = html.replace(old, new)

    # --- Fix 2: state cells had a fixed height, so long details overflowed into
    # the next row. Let them grow, and keep details on one line. Also let the
    # browser skip layout/paint of off-screen detail cards (content-visibility)
    # — with ~600 cards this is the difference between a frozen and a fluid page.
    css_fix = ("<style>/*fpi-fix*/.st{height:auto!important;min-height:25px;"
               "padding:2px 7px}.st .dt{white-space:nowrap}"
               "table.cond td,table.cond th{vertical-align:middle}"
               ".card{content-visibility:auto;contain-intrinsic-size:auto 560px}"
               ".tab-page{contain:layout style}"
               "</style>")
    html = html.replace("</head>", css_fix + "</head>", 1)

    # --- Fix 4 (performance): the template re-rendered EVERY tab on load and on
    # every filter keystroke (renderAll on #fSearch input). With 1,792 fares the
    # detail view alone builds ~600 cards, so the page froze constantly. Render
    # only the active tab, mark the rest dirty, render them on first activation,
    # and debounce the search box.
    # renderAll differs between template revisions (v9 added the score chart);
    # patch whichever variant this template carries.
    # NOTE: lazy per-tab rendering, the search/multi-select debounce and the
    # switchTab DOM eviction used to be patched in here; they now live in the
    # templates themselves (2026-08-04), so a template edit can no longer break
    # the build by moving an anchor line.
    perf_patches = [
        # --- Fix 6: Detay Analiz built ~600 cards (~100k DOM nodes) in one go;
        # paginate to 40-card chunks with a "Daha fazla göster" button.
        ("  Object.keys(groups).sort().forEach(k=>{",
         """  const _keys=Object.keys(groups).sort();
  let _shown=0;
  const _renderChunk=(n)=>{ _keys.slice(_shown,_shown+n).forEach(k=>{"""),
        ("""    host.appendChild(card);
  });
  $$(".alert-ico").forEach(el=>el.addEventListener("click",()=>openKnowHow(el.dataset.kh, el.dataset.khtext)));""",
         """    host.appendChild(card);
  });
  _shown=Math.min(_shown+n,_keys.length);
  const _old=document.getElementById("pdMore"); if(_old) _old.remove();
  if(_shown<_keys.length){
    const btn=document.createElement("button");
    btn.id="pdMore";
    btn.style.cssText="display:block;margin:14px auto;padding:10px 22px;border:1px solid #ccd5e0;border-radius:10px;background:#fff;cursor:pointer;font-weight:700;font-size:13px";
    btn.textContent="Daha fazla göster ("+(_keys.length-_shown)+" kart kaldı)";
    btn.addEventListener("click",()=>_renderChunk(40));
    host.appendChild(btn);
  }
  $$(".alert-ico").forEach(el=>el.addEventListener("click",()=>openKnowHow(el.dataset.kh, el.dataset.khtext)));
  };
  _renderChunk(40);"""),
        # NOTE: the former "Fix 5" (count Paid as a transition gain, not just
        # Included) used to be patched in here. It now lives in the templates'
        # own chainsFor, which was rewritten 2026-08-04 to average PER-UNIT
        # deltas instead of differencing tier averages — patching a snippet
        # that no longer exists would only break the build.
    ]
    for old, new in perf_patches:
        if old not in html:
            raise SystemExit(f"perf patch anchor not found: {old[:60]!r}")
        html = html.replace(old, new)      # all occurrences (anchors are exact code)

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

    # The admin editor writes a CSV that manual.py has to be able to import,
    # so the column list is injected from manual.py itself rather than retyped
    # in the template — two copies would drift the first time a right is added.
    from manual import COLUMNS as MANUAL_COLUMNS, RIGHT_COLS as MANUAL_RIGHTS
    html = html.replace("/*__MANUAL_COLS__*/[]",
                        json.dumps(MANUAL_COLUMNS, ensure_ascii=False))
    html = html.replace("/*__MANUAL_RIGHTS__*/[]",
                        json.dumps(MANUAL_RIGHTS, ensure_ascii=False))
    # The editor draws the SAME matrix as Detay Analiz, whose rows are keyed by
    # platform feature keys, while the manual CSV is keyed by column name. The
    # bridge is derived here from the one mapping that already exists, so a
    # renamed right cannot leave the editor writing into a column nobody reads.
    from branded_fare_scraper.amenities import AMENITY_DISPLAY as _DISP
    featmap = {FEATURE_KEY[k]: _DISP[k] for k in FEATURE_KEY if k in _DISP}
    html = html.replace("/*__MANUAL_FEATMAP__*/({})",
                        json.dumps(featmap, ensure_ascii=False))

    pat = re.compile(r"(/\*__EMBEDDED_DATA_START__\*/).*?(/\*__EMBEDDED_DATA_END__\*/)", re.DOTALL)
    new_html, n = pat.subn(lambda m: m.group(1) + embedded + m.group(2), html)
    if n != 1:
        raise SystemExit(f"Expected exactly 1 EMBEDDED marker pair, found {n}")
    out_html.write_text(new_html, encoding="utf-8")
    print(f"Injected {len(fares)} fares -> {out_html} ({out_html.stat().st_size//1024} KB)")


if __name__ == "__main__":
    main()
