"""Ubfly (OTA aggregator) — OTA fallback + cross-check. Priority 3.

Ubfly resells GDS/NDC content for ~all carriers and renders the full branded
fare comparison in the DOM, pre-populated for every flight in a single search
(no per-flight clicks needed). One OND search therefore serves every carrier on
that route, so results are cached per (from, to, date, cabin).

Deep-link (no form filling):
    /dis-hat-arama-sonuc?from=IST&to=LHR&ddate=DD.MM.YYYY&cabintype=2&adult=1&flightType=2

Each fare card (``div.domestic-brand-box``) carries:
  * ``.branded-title``  -> raw brand name (e.g. "Economy Saver")
  * ``onclick="onepagecheckout(…,'<IATA>','<FAREFAMILYCODE>','<name>','<CABIN>')"``
  * ``<ul><li>`` rules: "Baggage: N x K KG", "Cabin Baggage: N x K KG",
    "SEATSELECTION - at charge", "PRIORITY_BOARDING - not permitted", …
  * a delta price button ("0.00 / +39.91 / +79.80 USD") relative to the flight's
    base fare (shown in the row header).
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from datetime import date
from typing import Any, Optional

from ..amenities import classify_status_from_text, map_label_to_canonical
from ..models import (AmenityStatus, Cabin, CabinResult, Job, PriceType, RawAmenity,
                      RawBrand, RawMiles)
from ..normalization import (cabin_rank, detect_cabin, effective_cabin, ff_override,
                             ladder_metrics, regroup_brands_by_cabin)
from ..pricing import parse_price
from ..retry import CarrierAbsent, Forbidden, NoAvailabilityError
from .base import (SourceAdapter, extraction_diff, norm_airline_name, operator_matches,
                   register, resolve_airline_code)

_LOG = logging.getLogger("bfs")

BASE_URL = "https://www.ubfly.com/dis-hat-arama-sonuc"

# Ubfly cabintype query param per cabin (verified: economy=2).
CABIN_PARAM = {Cabin.ECONOMY: "2", Cabin.BUSINESS: "3"}
CABIN_FROM_STR = {
    "ECONOMY": Cabin.ECONOMY, "PREMIUM_ECONOMY": Cabin.PREMIUM_ECONOMY,
    "PREMIUMECONOMY": Cabin.PREMIUM_ECONOMY, "BUSINESS": Cabin.BUSINESS,
    "FIRST": Cabin.FIRST,
}

# Ubfly rule token -> canonical amenity key (None = ignore / handled elsewhere).
RULE_MAP = {
    "SEATSELECTION": "seat_selection",
    "LOUNGEACCESS": "lounge_access",
    "PRIORITY_BOARDING": "priority_boarding",
    "PRIORITY_SECURITY": "fast_track",
    "PRIORITY_CHECKIN": None,
    "CHANGE_AFTERDEPARTURE": "change",
    "CHANGE_BEFOREDEPARTURE": "change",
    "CANCEL_AFTERDEPARTURE": "refund",
    "CANCEL_BEFOREDEPARTURE": "refund",
    "REFUND": "refund",
    "CANCEL_NOSHOWFIRST": "no_show_refund",
    "CANCEL_NOSHOWSUBSEQUENT": "no_show_refund",
    "CHANGE_NOSHOWFIRST": "no_show_change",
    "CHANGE_NOSHOWSUBSEQUENT": "no_show_change",
    "SAMEDAYCHANGE": "same_day_earlier_flight",
    "MEAL": "meal",
    "WIFI": "wifi",
    "PET": "pet",
    "SPORT": "sports_equipment",
    "SPORTS": "sports_equipment",
    "SPORTSEQUIPMENT": "sports_equipment",
    "EXTRABAGGAGE": "extra_baggage",
}

# JS that returns each flight (tr.flight-item) with its base price and the
# branded-fare panel that follows it in the sortable results table.
_EXTRACT_JS = r"""
() => {
  const priceRe = /(\d[\d.,]*)\s*\.?\s*(\d{2})?\s*(USD|EUR|TRY|TL|GBP)/i;
  const out = [];
  const mains = [...document.querySelectorAll('tr.flight-item')];
  for (const main of mains) {
    const rowText = main.innerText || '';
    const headM = rowText.match(priceRe);
    const baseText = headM ? headM[0] : '';
    // Non-stop or not: the row's duration cell reads "Direct" vs "1 Transfers".
    // Read the ROW's own text only — the fare panel below it never carries this.
    const direct = /\bDirect\b/i.test(rowText) && !/Transfer/i.test(rowText);
    // The row's OWN airline identity: logo filename ("25px-QR.png") first, then
    // a flight-number prefix ("QR 120"). The fare boxes' IATA can belong to a
    // codeshare seller (QR-plated fares on BA metal), so it must not drive
    // carrier attribution on its own.
    let rowCarrier = '';
    const img = main.querySelector('img');
    const srcM = ((img && (img.getAttribute('src') || '')) || '')
        .match(/(?:\d+px-)?([A-Z0-9]{2})\.(?:png|svg|jpe?g|webp)/i);
    if (srcM) rowCarrier = srcM[1].toUpperCase();
    if (!rowCarrier) {
      const fm = rowText.match(/\b([A-Z][A-Z0-9])\s?-?\s?\d{2,4}\b/);
      if (fm) rowCarrier = fm[1].toUpperCase();
    }
    // Interline: the row prints who really OPERATES the flight under the
    // flight number ("QR 9711 / Operating Airline British Airways").
    let operating = '';
    const opM = rowText.match(/Operating\s*Airline\s*[:\-]?\s*([^\n\r|]{2,40})/i)
             || rowText.match(/Operated\s*by\s*[:\-]?\s*([^\n\r|]{2,40})/i);
    if (opM) operating = opM[1].trim();
    // Walk following sibling rows to this flight's branded panel.
    let panel = null;
    let el = main.nextElementSibling;
    for (let i = 0; i < 6 && el; i++) {
      if (/\bflight-item\b/.test(el.className || '')) break;   // next flight
      const found = el.querySelector ? el.querySelector('.branded-fares') : null;
      if (found) { panel = found; break; }
      el = el.nextElementSibling;
    }
    if (!panel) continue;
    const boxes = [...panel.querySelectorAll('.domestic-brand-box')];
    const brands = boxes.map(box => {
      const name = ((box.querySelector('.branded-title') || {}).innerText || '').trim();
      const oc = box.getAttribute('onclick') || '';
      const args = [...oc.matchAll(/`([^`]*)`/g)].map(m => m[1]);
      const lis = [...box.querySelectorAll('li')].map(li => (li.innerText || '').replace(/\s+/g, ' ').trim()).filter(Boolean);
      const btn = box.querySelector('button, .price-button-wrapper, strong');
      const btnText = btn ? (btn.innerText || '') : (box.innerText || '');
      const dm = btnText.match(/([+\-]?\s*\d[\d.,]*)\s*(USD|EUR|TRY|TL|GBP)/i);
      return {
        name: name,
        iata: args[5] || '',
        ffcode: args[6] || '',
        cabin: (args[8] || args[args.length - 1] || '').toUpperCase(),
        // Slots 0-4, 7 and 9+ are structured fields the site already hands us
        // and nothing has ever looked at them. Keep the whole vector in the raw
        // record so it can be mined offline instead of costing another live
        // visit to a source that challenges us for visiting too often.
        argv: args,
        lis: lis,
        priceText: dm ? dm[0] : ''
      };
    });
    out.push({ carrier: rowCarrier || (brands[0] && brands[0].iata) || '',
               fare_iata: (brands[0] && brands[0].iata) || '',
               operating: operating,
               direct: direct, baseText: baseText, brands: brands });
  }
  return out;
}
"""

#: Airline-identity helpers now live in ``sources.base`` so Ubfly and Trip.com
#: apply ONE interline rule; re-exported here for existing importers.
_norm_airline_name = norm_airline_name
_operator_matches = operator_matches

#: Markers of an interactive bot-challenge page (detected, never bypassed).
_CHALLENGE_RE = re.compile(
    r"verify you are (a )?human|performing security verification|just a moment|"
    r"attention required|checking your browser|access denied|you have been blocked|"
    r"insan olduğunuzu doğrulayın", re.I)

_BAG_RE = re.compile(r"(cabin baggage|personal item|baggage)\s*:?\s*(\d+)\s*x\s*(\d+)\s*kg", re.I)
_RULE_RE = re.compile(r"^([A-Z][A-Z_]{2,})\s*-\s*(.+)$")
# English "mile(s)" or Turkish "mil" ("%25 Ekstra Mil", "30 PERCENT EXTRA MILES").
_MILES_RE = re.compile(r"\bmil(?:e|es)?\b", re.I)
_PERCENT_RE = re.compile(r"%|percent|yüzde", re.I)


@register
class Ubfly(SourceAdapter):
    name = "Ubfly"
    # Priority 3 (Round 15): Trip.com became the primary OTA (richer, airline-
    # sourced fare cards). Ubfly stays registered as the fallback AND as the
    # independent cross-check source; it still outranks Enuygun, whose compact
    # items are the last resort and only enrich via the cross-source merge.
    priority = 3
    needs_browser = True

    # Shared across all worker pages: one search per (from,to,date,cabin) serves
    # every carrier on that route.
    _cache: dict[str, list[dict]] = {}
    _locks: dict[str, asyncio.Lock] = {}
    #: (origin, destination) pairs Ubfly redirected back to the search form for
    #: (unserved points like ZYR = Brussels Midi RAIL) — skip every further
    #: date/carrier on them instead of burning a 45s timeout per search.
    _dead_routes: set = set()
    #: When set (by the runner) every ACTUAL page search saves a full-page PNG
    #: here — collection-moment evidence; cache hits reuse the existing shot.
    evidence_dir = None
    #: Set once the site answers with an interactive bot challenge: every later
    #: search then returns instantly instead of burning the 45s selector wait
    #: (the challenge cost 220-500s per unit in the pilot). Cleared per run by
    #: ``reset_state``, so Ubfly comes back the day the challenge lifts.
    _challenge_disabled: bool = False
    #: Seconds to spend deciding whether a freshly-loaded page is a challenge.
    #: Long enough for Cloudflare's non-interactive check to clear on its own:
    #: at 5 s the probe only ever saw the interstitial and disabled the source
    #: for the run, while the real page (482 flights) arrived by ~12 s.
    challenge_probe_s: float = 18.0
    #: Ubfly is a TOP-UP source only: one worker, slowly. Set from Config.
    min_interval_s: float = 6.0
    _gate_lock = None
    _slot = None
    _last_hit_at: float = 0.0

    @classmethod
    def _primitives(cls) -> None:
        if cls._gate_lock is None:
            cls._gate_lock = asyncio.Lock()
        if cls._slot is None:
            cls._slot = asyncio.Semaphore(1)      # never more than one at a time

    @classmethod
    async def _gate(cls) -> None:
        """One slow, global cadence for Ubfly — it is a courtesy call, not a crawl."""
        cls._primitives()
        async with cls._gate_lock:
            wait = (cls._last_hit_at + cls.min_interval_s * random.uniform(0.8, 1.3)) \
                - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            cls._last_hit_at = time.monotonic()

    @classmethod
    def reset_state(cls) -> None:
        cls._challenge_disabled = False
        cls._gate_lock = None
        cls._slot = None
        cls._last_hit_at = 0.0

    def supports(self, carrier: str) -> bool:
        return True  # aggregator — covers any carrier that Ubfly resells

    @staticmethod
    def _matches_carrier(flight: dict, target: str) -> bool:
        """A flight belongs to ``target`` only if no identity signal contradicts.

        ``carrier`` = the row's own identity (logo / flight number);
        ``fare_iata`` = the fare boxes' seller. A QR-plated fare on a BA-operated
        row (row=BA, fare=QR) is a codeshare carrying the PARTNER's fare ladder —
        it must not be attributed to QR (that produced BIZPROMO/ECONSEL-style
        foreign families under QR on ex-UK routes).

        ``operating`` = the airline that actually FLIES the row ("Operating
        Airline British Airways" under the flight number). User rule: if another
        carrier operates the flight, it is interline and must be dropped — an
        LH-marketed, Aegean-operated row sells Aegean's product, not LH's.
        """
        rowc = (flight.get("carrier") or "").upper()
        fic = (flight.get("fare_iata") or "").upper()
        if rowc and rowc != target:
            return False
        if fic and fic != target:
            return False
        if not _operator_matches(flight.get("operating"), target):
            return False
        return rowc == target or fic == target

    def cabins_for(self, job: Job) -> list[Cabin]:
        return [Cabin.ECONOMY, Cabin.BUSINESS]

    def best_buckets(self, carrier_flights: list[dict], search_cabin: Cabin,
                     target: str) -> dict[Cabin, list[RawBrand]]:
        """Pick the best flight's re-bucketed ladder for one searched cabin.

        Every carrier flight on the page is inspected and scored: MOST packages
        (user rule), then most cabins covered, fewest implausible (>3x) price
        jumps, smallest worst step, cheapest base. **Direct wins**: if any
        non-stop flight yields a ladder for the searched cabin, the
        representative flight is chosen among the direct ones only (user rule:
        "if there is a direct flight, pick it — but still inspect every flight
        and take the one with the most packages"); otherwise all flights compete.

        Side-picks: a higher cabin the winner does not carry (Premium Economy,
        or a Business upgrade-leak inside the Economy search) is taken from the
        next flight that offers it — direct flights first, but a connection may
        still supply it, because losing a whole cabin is worse than a stop.
        Shared by ``fetch_search`` and the screenshot-verification harness so
        both apply IDENTICAL selection. Flight dicts stored by older runs carry
        no ``direct`` key; they simply count as non-direct.
        """
        keep_pe = search_cabin == Cabin.ECONOMY
        scored: list[tuple[tuple, dict[Cabin, list[RawBrand]], bool]] = []
        for f in carrier_flights:
            buckets = regroup_brands_by_cabin(
                self._to_raw_brands(f, search_cabin), search_cabin,
                keep_pe=keep_pe, carrier=target)
            if not buckets:
                continue
            allb = [b for bs in buckets.values() for b in bs]
            bad, worst = ladder_metrics(allb)
            base_val, _, _ = parse_price(_clean_price(f.get("baseText", "")))
            scored.append(((len(allb), len(buckets), -bad, -worst,
                            -(base_val if base_val is not None else float("inf"))),
                           buckets, bool(f.get("direct"))))
        scored.sort(key=lambda t: t[0], reverse=True)
        order = list(range(len(scored)))
        # Direct flights that actually price the searched cabin get the pick.
        direct = [i for i in order if scored[i][2] and scored[i][1].get(search_cabin)]
        primary = direct or order
        if not primary:
            return {}
        best = dict(scored[primary[0]][1])
        # Preference-ordered scan for the remaining cabins: direct first, rest after.
        rest = [i for i in primary[1:]] + [i for i in order if i not in set(primary)]
        if keep_pe:
            for i in rest:
                for cab, bs in scored[i][1].items():
                    if bs and cab not in best and cabin_rank(cab) > cabin_rank(search_cabin):
                        best[cab] = bs
        return best

    # ------------------------------------------------------------------ #
    async def fetch_search(self, page, job: Job, departure: date, return_date: date) -> list[CabinResult]:
        results: dict[Cabin, CabinResult] = {}
        target = job.carrier.strip().upper()
        any_flights = False
        searched = self.cabins_for(job)
        #: cabins filled by an upgrade leak from ANOTHER cabin's search — the
        #: dedicated search for that cabin (ct3 for Business) overrides them.
        provisional: set[Cabin] = set()
        for search_cabin in searched:
            flights = await self._search(page, job.origin, job.destination, departure, search_cabin)
            if flights:
                any_flights = True
            carrier_flights = [f for f in flights if self._matches_carrier(f, target)]
            if not carrier_flights:
                continue
            # Premium-Economy families (e.g. BA World Traveller Plus, AC PL/PF)
            # leak into the Economy ladder, so recover PE from the Economy search
            # only (avoids double-counting it in the Business search).
            best = self.best_buckets(carrier_flights, search_cabin, target)
            for cab, bs in best.items():
                if not bs:
                    continue
                dedicated = cab == search_cabin
                # A cabin's own search always beats a leak picked up in another
                # search; a leak only fills a cabin nothing else provided.
                if cab in results and not (dedicated and cab in provisional):
                    continue
                results[cab] = CabinResult(cabin=cab, departure=departure,
                                           return_date=return_date, brands=bs)
                if dedicated:
                    provisional.discard(cab)
                elif cab in searched:
                    provisional.add(cab)
        if not results:
            if any_flights:
                # Route has flights but none for this carrier -> it doesn't fly
                # this OND; don't waste the rest of the date window.
                raise CarrierAbsent(f"Ubfly: {target} not on route {job.route}")
            # No flights at all on this date -> let the window advance to the next.
            raise NoAvailabilityError(f"Ubfly: no flights {job.route} {departure}")
        return list(results.values())

    # ------------------------------------------------------------------ #
    async def _search(self, page, origin: str, destination: str, departure: date,
                      cabin: Cabin) -> list[dict]:
        if Ubfly._challenge_disabled or (origin, destination) in self._dead_routes:
            return []                     # challenged for this run: fail instantly
        param = CABIN_PARAM.get(cabin, "2")
        key = f"{origin}|{destination}|{departure.isoformat()}|{param}"
        if key in self._cache:
            return self._cache[key]
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            if key in self._cache:
                return self._cache[key]
            flights = await self._do_search(page, origin, destination, departure, param)
            if flights:                       # only cache real results; a transient
                self._cache[key] = flights    # empty/timeout must not poison the route
            return flights

    async def _challenge_present(self, page) -> bool:
        """Is this an interactive bot-challenge page? (decided in ~5s, no bypass)

        We never attempt to solve or evade a challenge — the only goal is to
        notice it immediately so the run stops paying the 45s selector timeout
        for a page that will never render results.
        """
        deadline = self.challenge_probe_s
        saw_challenge = False
        while deadline > 0:
            try:
                if await page.evaluate("() => !!document.querySelector('.domestic-brand-box, "
                                       "tr.flight-item, form[action*=arama]')"):
                    return False               # real site markup -> not a challenge
                marked = await page.evaluate(
                    "() => !!document.querySelector('#cf-chl-widget, [id^=cf-chl], "
                    "[class*=cf-challenge], #challenge-form, "
                    "iframe[src*=\"challenges.cloudflare.com\"]')")
                probe = (await page.title() or "") + " " + await page.evaluate(
                    "() => (document.body.innerText || '').slice(0, 300)")
            except Exception:  # noqa: BLE001 - a mid-navigation evaluate can throw
                marked, probe = False, ""
            # Seeing the interstitial is NOT the verdict. Ubfly fronts the page
            # with Cloudflare's non-interactive check, which clears itself in a
            # few seconds: probing for 5 s and returning True on first sight
            # disabled Ubfly for the whole run, while waiting 12 s by hand gave
            # the real page with 482 flights (2026-08-02). Only a wall that is
            # STILL there when the window closes counts as a challenge.
            hit = _CHALLENGE_RE.search(probe)
            if (marked or hit) and not saw_challenge:
                # Name the evidence. "bot challenge" with nothing behind it sent
                # this investigation to the site when the fault was local; a
                # verdict that cannot be checked is not a diagnosis.
                _LOG.info("Ubfly challenge signal: marker=%s match=%r probe=%r",
                          marked, hit.group(0) if hit else None, probe[:160])
            saw_challenge = saw_challenge or marked or bool(hit)
            await asyncio.sleep(0.5)
            deadline -= 0.5
        if saw_challenge:
            _LOG.info("Ubfly: challenge still standing after %.0fs", self.challenge_probe_s)
        return saw_challenge

    async def _do_search(self, page, origin, destination, departure, param) -> list[dict]:
        ddate = departure.strftime("%d.%m.%Y")
        url = (f"{BASE_URL}?from={origin}&to={destination}&ddate={ddate}"
               f"&cabintype={param}&adult=1&flightType=2")
        await self._gate()
        await page.goto(url, wait_until="domcontentloaded")
        # Fast challenge check FIRST: an interactive "Verify you are human" page
        # never resolves on its own, so waiting the full 45s selector timeout on
        # every search (and every retry) costs minutes per unit for nothing.
        if await self._challenge_present(page):
            if not Ubfly._challenge_disabled:
                _LOG.warning("Ubfly disabled for this run — bot challenge "
                             "(no bypass attempted); other sources continue")
            Ubfly._challenge_disabled = True
            raise Forbidden("Ubfly Cloudflare / bot challenge")
        try:
            # Cards are pre-rendered but hidden -> wait for *attached*, not visible.
            # Cloudflare's managed challenge auto-resolves in a headful browser
            # within a few seconds, after which the cards appear.
            await page.wait_for_selector(".domestic-brand-box", state="attached", timeout=45000)
        except Exception:
            probe = ""
            try:
                probe = (await page.title() or "") + " " + \
                    (await page.evaluate("() => (document.body.innerText || '').slice(0, 300)"))
            except Exception:
                pass
            if re.search(r"just a moment|attention required|access denied|"
                         r"you have been blocked|verify you are (a )?human", probe, re.I):
                raise Forbidden("Ubfly Cloudflare / bot challenge")
            # Unserved point: the site bounced the deep-link back to the search
            # form (URL lost the results path / destination left blank). Mark
            # the pair dead so no further date/carrier wastes a 45s timeout.
            try:
                if "dis-hat-arama-sonuc" not in (page.url or ""):
                    Ubfly._dead_routes.add((origin, destination))
                    _LOG.info("Ubfly: %s-%s not served (form bounce) — skipping route", origin, destination)
            except Exception:  # noqa: BLE001
                pass
            return []                                      # genuine "no results"
        await asyncio.sleep(1.0)                            # let the list settle
        try:
            flights = await self._extract_verified(
                page, f"{origin}-{destination} {ddate} ct{param}")
        except Exception as e:  # noqa: BLE001
            _LOG.debug("Ubfly extract failed: %s", e)
            return []
        if flights and Ubfly.evidence_dir is not None:
            try:
                shot = Ubfly.evidence_dir / f"{origin}-{destination}_{ddate.replace('.', '')}_ct{param}.png"
                if not shot.exists():
                    await page.screenshot(path=str(shot), full_page=False)
            except Exception as e:  # noqa: BLE001 - evidence must never break the scrape
                _LOG.debug("Ubfly evidence shot failed: %s", e)
        return flights or []

    # ------------------------------------------------------------------ #
    #: Seconds to let the table settle before re-reading a disagreeing parse.
    verify_delay_s: float = 2.0

    async def _extract_verified(self, page, tag: str = "") -> list[dict]:
        """Parse the loaded results page TWICE and cross-check the two reads.

        User rule: "yazdığın ile ekranda yazanı cross check". A payload read
        while the results table is still re-rendering can miss rows or carry a
        stale price, so the same extraction is run again on the same page (no
        navigation — cheap). If the reads disagree we wait and read once more;
        a still-disagreeing page is logged as a WARNING and the LATER read is
        kept, since it is the more settled one.
        """
        first = await page.evaluate(_EXTRACT_JS) or []
        second = await page.evaluate(_EXTRACT_JS) or []
        if not extraction_diff(first, second):
            return second
        await asyncio.sleep(self.verify_delay_s)
        third = await page.evaluate(_EXTRACT_JS) or []
        diff = extraction_diff(second, third)
        if diff:
            _LOG.warning("Ubfly double-parse mismatch %s: %s", tag, "; ".join(diff[:4]))
        return third

    # ------------------------------------------------------------------ #
    def _to_raw_brands(self, flight: dict, requested_cabin: Cabin) -> list[RawBrand]:
        # Ubfly's box price is a DELTA relative to the flight's base fare (the
        # first box is "0.00" = the base), so each package's own absolute is
        # base + delta. Re-bucketing to the true cabin is left to the caller.
        carrier = (flight.get("carrier") or "").upper() or None
        base_val, _bt, base_cur = parse_price(_clean_price(flight.get("baseText", "")))
        out: list[RawBrand] = []
        for order, b in enumerate(flight.get("brands", [])):
            box_cabin = CABIN_FROM_STR.get((b.get("cabin") or "").replace(" ", ""))
            name = b.get("name") or ""
            ff = b.get("ffcode") or None
            ov = ff_override(carrier, ff)
            if ov:
                # Tabled carrier code: the code decides name + cabin (Ubfly's
                # display name for these is junk, e.g. AC EL -> "Economy Light").
                name, cabin = ov[0], ov[1]
            else:
                # A box whose own two labels contradict each other (economy-named
                # but structurally tagged Business) is untrustworthy junk either
                # way — drop it. Two exemptions, both real patterns:
                #   * PE anywhere in the conflict — an "economy" tag on a PE fare
                #     family is the normal leak, not a conflict;
                #   * either label naming a cabin ABOVE the searched one — that
                #     is an upgrade leak (LH ATH-FRA economy search sells a
                #     BUSINESS box on the same row); keep it, and let
                #     effective_cabin bucket it under its true cabin. A box
                #     naming a LOWER cabin (economy box in a Business search)
                #     stays dropped as noise.
                name_cab = detect_cabin(name)
                if (name_cab and box_cabin and name_cab != box_cabin
                        and Cabin.PREMIUM_ECONOMY not in (name_cab, box_cabin)
                        and max(cabin_rank(name_cab), cabin_rank(box_cabin))
                        <= cabin_rank(requested_cabin)):
                    continue
                cabin = effective_cabin(name, ff, box_cabin, requested_cabin, carrier=carrier)
            delta_val, _dt, delta_cur = parse_price(_clean_price(b.get("priceText", "")))
            currency = delta_cur or base_cur
            if base_val is not None and delta_val is not None:
                absolute = round(base_val + delta_val, 2)
            elif delta_val is not None:
                absolute = delta_val
            else:
                absolute = base_val
            if base_val is not None and absolute is not None and absolute < base_val - 0.01:
                _LOG.debug("Ubfly price check: %s abs %.2f < base %.2f (delta %s)",
                           name, absolute, base_val, delta_val)
            amenities, miles, desc = self._parse_rules(b.get("lis", []))
            out.append(RawBrand(
                raw_brand_name=name or (ff or f"Fare {order+1}"),
                cabin=cabin,
                screen_order=order,
                price_value=absolute,
                price_type=PriceType.ABSOLUTE,       # we already resolved the absolute
                currency=currency,
                display_price_text=(b.get("priceText") or "").strip(),
                fare_family_code=ff,
                description="; ".join(desc),
                amenities=amenities,
                miles=miles,
            ))
        return out

    def _parse_rules(self, lis: list[str]) -> tuple[list[RawAmenity], RawMiles, list[str]]:
        amenities: list[RawAmenity] = []
        miles = RawMiles()
        desc: list[str] = []
        for li in lis:
            # Miles accrual line. "%25 Ekstra Mil" / "50% Extra Miles" are BONUS
            # percentages, not earned-mile counts — route the number accordingly.
            if _MILES_RE.search(li) and "baggage" not in li.lower():
                miles.mileage_available = True
                m = re.search(r"(\d[\d.,]*)", li)
                if m and any(ch.isdigit() for ch in m.group(1)):
                    try:
                        val = float(m.group(1).replace(",", ""))
                        if _PERCENT_RE.search(li):
                            miles.bonus_percent = val
                        else:
                            miles.miles_earned = val
                    except ValueError:
                        pass
                continue
            # Baggage lines: "Baggage: 1 x 23 KG" / "Cabin Baggage: 1 x 8 KG".
            bag = _BAG_RE.search(li)
            if bag:
                kind = bag.group(1).lower()
                pieces, kg = int(bag.group(2)), int(bag.group(3))
                key = ("cabin_baggage" if "cabin" in kind
                       else None if "personal" in kind else "checked_baggage")
                if key is None:
                    continue
                status = (AmenityStatus.INCLUDED if (pieces > 0 and kg > 0)
                          else AmenityStatus.NOT_INCLUDED)
                amenities.append(RawAmenity(raw_label=li, status=status,
                                            raw_value=f"{pieces} x {kg} KG", canonical_key=key))
                continue
            # "RULETYPE - status" lines.
            rule = _RULE_RE.match(li)
            if rule:
                rtype = rule.group(1).upper().replace(" ", "")
                statustext = rule.group(2).strip()
                key = RULE_MAP.get(rtype, "__unknown__")
                if key == "__unknown__":
                    key = map_label_to_canonical(rtype)
                if not key:
                    desc.append(li)
                    continue
                amenities.append(RawAmenity(raw_label=li,
                                            status=classify_status_from_text(statustext),
                                            raw_value=statustext, canonical_key=key))
                continue
            # Free-text benefit line (e.g. "Seat selection before check-in",
            # "Food Menu", "Catering"). In Ubfly's panel a plainly-listed line is
            # an *included* feature of the fare, so an otherwise-unknown status
            # defaults to Included (an explicit "at charge"/"not permitted" still
            # wins via classify_status_from_text).
            key = map_label_to_canonical(li)
            if key:
                st = classify_status_from_text(li)
                if st == AmenityStatus.UNKNOWN:
                    st = AmenityStatus.INCLUDED
                amenities.append(RawAmenity(raw_label=li, status=st,
                                            raw_value=li, canonical_key=key))
            else:
                desc.append(li)
        return amenities, miles, desc


def _clean_price(text: str) -> str:
    """Join cents split across nodes: '319 .08 USD' -> '319.08 USD'."""
    if not text:
        return ""
    return re.sub(r"(\d)\s*\.\s*(\d)", r"\1.\2", text).strip()
