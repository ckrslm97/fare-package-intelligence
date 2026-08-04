#!/usr/bin/env python3
"""Which carriers does Enuygun ACTUALLY return on a route, and which of them
carry branded packages? Distinguishes "our matching dropped the carrier" from
"the source never sells it here" — the two look identical in the run log
("no branded packages for XX").

    python probe_carrier_present.py AYT MAN 2027-06-15 LS
"""
from __future__ import annotations
import asyncio, json, sys, collections
from datetime import date
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from branded_fare_scraper.browser_pool import BrowserPool
from branded_fare_scraper.config import Config
from branded_fare_scraper.sources.enuygun import Enuygun


async def main():
    o, d, ds = sys.argv[1], sys.argv[2], sys.argv[3]
    want = (sys.argv[4] if len(sys.argv) > 4 else "").upper()
    dep = date.fromisoformat(ds)
    cfg = Config(browser_channel="chrome", headless=True)
    pool = BrowserPool(cfg)
    await pool.start()
    src = Enuygun()
    try:
        async with pool.page() as page:
            o_slug, d_slug = await src._ensure_slugs(page, o, d)
            bodies = []

            async def on_resp(resp):
                try:
                    if "async-result" in resp.url and resp.status == 200:
                        t = await resp.text()
                        if len(t) > 5000:
                            bodies.append(t)
                except Exception:
                    pass

            page.on("response", on_resp)
            url = (f"https://www.enuygun.com/ucak-bileti/arama/"
                   f"{o_slug}-{d_slug}-{o.lower()}-{d.lower()}/"
                   f"?gidis={dep.strftime('%d.%m.%Y')}&yetiskin=1&cocuk=0&bebek=0")
            await page.goto(url, wait_until="domcontentloaded")
            await src._cookies(page)
            await asyncio.sleep(18)
    finally:
        await pool.stop()

    if not bodies:
        print("veri gelmedi")
        return
    data = json.loads(max(bodies, key=len))
    flights = (data.get("flights") or {}).get("departure") or []
    seen = collections.Counter()
    withpkg = collections.Counter()
    for f in flights:
        for seg in (f.get("segments") or []):
            code = (seg.get("marketing_airline") or seg.get("airline") or "").upper()
            if code:
                seen[code] += 1
        first = (f.get("segments") or [{}])[0]
        code = (first.get("marketing_airline") or first.get("airline") or "").upper()
        if f.get("provider_packages"):
            withpkg[code] += 1
    print(f"{o}-{d} {dep}: {len(flights)} uçuş\n")
    print(f"{'KOD':5s} {'segmentte':>10s} {'paketli uçuş':>13s}")
    for code, n in seen.most_common():
        print(f"{code:5s} {n:10d} {withpkg.get(code,0):13d}")
    if want:
        print()
        if want not in seen:
            print(f"=> {want} bu rotada Enuygun sonuçlarında HİÇ YOK (kaynak sınırı, bizim hatamız değil)")
        elif not withpkg.get(want):
            print(f"=> {want} uçuyor ama HİÇBİR uçuşunda branded paket yok (kaynak sınırı)")
        else:
            print(f"=> {want} paketli uçuş sunuyor -> BİZİM eşleştirmemiz düşürüyor, HATA")


if __name__ == "__main__":
    asyncio.run(main())
