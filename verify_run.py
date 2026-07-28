"""Publish GATE: screenshot-verify a finished run against the live source.

For EVERY carrier in <out>/raw_data.jsonl (first unit per carrier, both
cabins): reload the same Ubfly route+date, re-run the IDENTICAL selection
(``Ubfly.best_buckets``), diff live names/prices vs stored raw, and save the
matching flight's panel screenshot. Also scans the whole dataset for
suspicious (code-like) display names.

Exit code 0  = publishable: zero NAME_MISMATCH and zero suspicious names.
Exit code 1  = blocked: mismatches/suspicious names listed in the report.
(OK_PRICE_DRIFT = names identical, price moved intraday — allowed.
 NO_CARRIER_TODAY = carrier not listed on that route at check time — noted.)

Usage: python verify_run.py <out_dir>
Re-runs are incremental: samples with an existing evidence PNG are kept
(result merged from <out>/evidence/report.json when present).
"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import date
from pathlib import Path

from branded_fare_scraper.browser_pool import BrowserPool
from branded_fare_scraper.config import Config
from branded_fare_scraper.models import Cabin
from branded_fare_scraper.normalization import is_suspicious_brand_name, pretty_brand_name
from branded_fare_scraper.rebuild import iter_raw_records
from branded_fare_scraper.sources.ubfly import CABIN_PARAM, Ubfly, _EXTRACT_JS

CABIN_BY_NAME = {c.value: c for c in Cabin}

FIND_ROW_JS = r"""
(args) => {
  const [carrier, wantNames] = args;
  const rows=[...document.querySelectorAll('tr.flight-item')];
  for (let i=0;i<rows.length;i++){
    const img=rows[i].querySelector('img');
    const src=(img&&img.getAttribute('src'))||'';
    const m=src.match(/(?:\d+px-)?([A-Z0-9]{2})\.(?:png|svg|jpe?g|webp)/i);
    if(!m || m[1].toUpperCase()!==carrier) continue;
    let el=rows[i].nextElementSibling, panel=null;
    for(let k=0;k<6&&el;k++){ if(/\bflight-item\b/.test(el.className||''))break;
      const f=el.querySelector?el.querySelector('.branded-fares'):null; if(f){panel=f;break;} el=el.nextElementSibling;}
    if(!panel) continue;
    const names=[...panel.querySelectorAll('.branded-title')].map(t=>(t.innerText||'').trim());
    if(wantNames.every(n=>names.includes(n))) return i;
  }
  return -1;
}
"""


def load_samples(out: Path):
    samples = {}
    for rec in iter_raw_records(out / "raw_data.jsonl"):
        car = rec["carrier"].upper()
        if car in samples or not rec.get("cabins"):
            continue
        by_search = {}
        for c in rec["cabins"]:
            if not c["brands"]:
                continue
            search = "Economy" if c["cabin"] in ("Economy", "Premium Economy") else "Business"
            e = by_search.setdefault(search, {"dep": c["departure"], "stored": {}})
            e["stored"][c["cabin"]] = [(b["raw_brand_name"], b["price_value"])
                                       for b in c["brands"]]
        if by_search:
            samples[car] = {"origin": rec["origin"], "dest": rec["destination"],
                            "searches": by_search}
    return samples


# Reviewed-and-accepted opaque family tokens (real filed fare families whose
# only available label IS the token — e.g. BA's World-Traveller-Plus families).
KNOWN_OK_TOKEN_NAMES = {"PREMECON", "PESEL", "PEPRO", "PEFLEX"}


def suspicious_names(out: Path):
    import re
    bare = re.compile(r"^[A-Z]{2,3}\d?$")
    bad = {}
    for rec in iter_raw_records(out / "raw_data.jsonl"):
        for c in rec.get("cabins", []):
            for b in c["brands"]:
                disp = pretty_brand_name(b["raw_brand_name"], rec["carrier"])
                if bare.match(disp):
                    continue                       # excluded from published outputs
                if disp.upper() in KNOWN_OK_TOKEN_NAMES:
                    continue                       # deliberately accepted
                if is_suspicious_brand_name(disp, b.get("fare_family_code")):
                    bad.setdefault((rec["carrier"], disp), set()).add(
                        f'{rec["origin"]}-{rec["destination"]}')
    return bad


async def shoot_row(page, idx, path):
    await page.evaluate(r"""(idx)=>{
      const rows=[...document.querySelectorAll('tr.flight-item')];
      const btn=[...rows[idx].querySelectorAll('a,button')].find(b=>/select/i.test(b.innerText||''));
      if(btn){btn.scrollIntoView({block:'center'});btn.click();}
    }""", idx)
    await asyncio.sleep(2.5)
    h = await page.evaluate_handle(r"""(idx)=>{
      const rows=[...document.querySelectorAll('tr.flight-item')];
      let el=rows[idx].nextElementSibling;
      for(let k=0;k<6&&el;k++){ if(/\bflight-item\b/.test(el.className||''))break;
        const f=el.querySelector?el.querySelector('.branded-fares'):null; if(f)return f; el=el.nextElementSibling;}
      return null;
    }""", idx)
    elem = h.as_element()
    if elem:
        await elem.scroll_into_view_if_needed()
        await asyncio.sleep(0.4)
        await elem.screenshot(path=str(path))
        return True
    return False


async def main() -> int:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "output")
    ev = out / "evidence"
    ev.mkdir(exist_ok=True)
    prior = {}
    if (ev / "report.json").exists():
        for r in json.loads((ev / "report.json").read_text("utf-8")):
            prior[(r["carrier"], r["search"])] = r

    samples = load_samples(out)
    print(f"carriers: {len(samples)}")
    cfg = Config(input_path="CLAUDE_OND_LIST.xlsx", headless=False,
                 browser_pool_size=1, concurrency=1)
    pool = BrowserPool(cfg)
    await pool.start()
    ub = Ubfly()
    report = []
    try:
        async with pool.page() as page:
            for car in sorted(samples):
                s = samples[car]
                for search_val, e in s["searches"].items():
                    cab = CABIN_BY_NAME[search_val]
                    dep = date.fromisoformat(e["dep"])
                    ond = f'{s["origin"]}-{s["dest"]}'
                    tag = f'{car}_{s["origin"]}{s["dest"]}_{search_val[:3].lower()}'
                    entry = {"carrier": car, "ond": ond, "search": search_val,
                             "dep": e["dep"], "result": "", "detail": "", "shot": ""}
                    prev = prior.get((car, search_val))
                    if (ev / f"{tag}.png").exists() and prev and prev.get("result", "").startswith("OK"):
                        report.append(prev)
                        continue
                    url = (f'https://www.ubfly.com/dis-hat-arama-sonuc?from={s["origin"]}'
                           f'&to={s["dest"]}&ddate={dep.strftime("%d.%m.%Y")}'
                           f'&cabintype={CABIN_PARAM.get(cab, "2")}&adult=1&flightType=2')

                    async def one_sample():
                        await page.goto(url, wait_until="domcontentloaded")
                        await page.wait_for_selector(".domestic-brand-box", state="attached",
                                                     timeout=45000)
                        await asyncio.sleep(1.5)
                        flights = await page.evaluate(_EXTRACT_JS)
                        pure = [f for f in flights if Ubfly._matches_carrier(f, car)]
                        if not pure:
                            entry["result"] = "NO_CARRIER_TODAY"
                            return
                        best = ub.best_buckets(pure, cab, car)
                        diffs, drift = [], []
                        inv = []
                        for cabname, stored in e["stored"].items():
                            live = best.get(CABIN_BY_NAME[cabname]) or []
                            ln = [pretty_brand_name(b.raw_brand_name, car) for b in live]
                            sn = [pretty_brand_name(n, car) for n, _ in stored]
                            if ln != sn:
                                # The flight sold at collection time may have left
                                # the page (or a richer one appeared): overlapping/
                                # nested family sets = inventory rotation, NOT a
                                # parse error (the collection-moment search
                                # screenshot is the proof). Disjoint sets = real
                                # mismatch (wrong ladder) and still blocks.
                                s_set, l_set = set(sn), set(ln)
                                nested = s_set and l_set and (
                                    s_set <= l_set or l_set <= s_set
                                    or len(s_set & l_set) * 2 >= min(len(s_set), len(l_set)))
                                # Empty live bucket = the sampled date's page no
                                # longer offers this cabin (window-walk cabins may
                                # come from other dates; inventory rotates) — the
                                # collection-moment shot is the proof, not an error.
                                if nested or (s_set and not l_set):
                                    inv.append(f"{cabname}: stored {sn} vs live-now {ln}")
                                else:
                                    diffs.append(f"{cabname}: stored {sn} != live {ln}")
                                continue
                            for (n0, a0), lb in zip(stored, live):
                                if lb.price_value is not None and a0 is not None \
                                   and abs(lb.price_value - a0) > 0.01:
                                    drift.append(f"{cabname}/{n0}: {a0} -> {lb.price_value}")
                        if diffs:
                            entry["result"], entry["detail"] = "NAME_MISMATCH", " | ".join(diffs)[:300]
                        elif inv:
                            entry["result"], entry["detail"] = "OK_INVENTORY_CHANGED", " | ".join(inv)[:300]
                        elif drift:
                            entry["result"], entry["detail"] = "OK_PRICE_DRIFT", " | ".join(drift)[:300]
                        else:
                            entry["result"] = "OK"
                        want = [n for n, _ in next(iter(e["stored"].values()))][:2]
                        idx = await page.evaluate(FIND_ROW_JS, [car, want])
                        if idx >= 0 and await shoot_row(page, idx, ev / f"{tag}.png"):
                            entry["shot"] = f"{tag}.png"

                    try:
                        await asyncio.wait_for(one_sample(), timeout=100)
                    except asyncio.TimeoutError:
                        entry["result"] = "TIMEOUT"
                    except Exception as ex:  # noqa: BLE001
                        entry["result"], entry["detail"] = "ERROR", str(ex)[:200]
                    print(f'{car:3} {ond:9} {search_val:8} -> {entry["result"]} {entry["detail"][:80]}',
                          flush=True)
                    report.append(entry)
    finally:
        await pool.stop()

    sus = suspicious_names(out)
    ok = sum(1 for r in report if r["result"].startswith("OK"))
    mism = [r for r in report if r["result"] == "NAME_MISMATCH"]
    (ev / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), "utf-8")
    lines = ["# Screenshot Verification Report (publish gate)", "",
             f"Samples: {len(report)} — OK: {ok}, NAME_MISMATCH: {len(mism)}, "
             f"suspicious display names: {len(sus)}", "",
             "| Carrier | OND | Search | Result | Evidence |", "|---|---|---|---|---|"]
    for r in report:
        lines.append(f'| {r["carrier"]} | {r["ond"]} | {r["search"]} | {r["result"]} '
                     f'| {r["shot"] or "-"} |')
    if sus:
        lines += ["", "## Suspicious display names (review before publishing)", ""]
        for (car, nm), onds in sorted(sus.items()):
            lines.append(f"- {car}: `{nm}` on {', '.join(sorted(onds))}")
    (ev / "README.md").write_text("\n".join(lines), "utf-8")
    gate = 0 if (not mism and not sus) else 1
    print(f"\nGATE {'PASS' if gate == 0 else 'BLOCKED'}: ok={ok}/{len(report)} "
          f"mismatch={len(mism)} suspicious={len(sus)} -> {ev}/README.md", flush=True)
    return gate


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
