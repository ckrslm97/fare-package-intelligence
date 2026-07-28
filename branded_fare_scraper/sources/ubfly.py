"""Ubfly (OTA aggregator) — primary OTA fallback. Priority 2.

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
import re
from datetime import date
from typing import Any, Optional

from ..amenities import classify_status_from_text, map_label_to_canonical
from ..models import (AmenityStatus, Cabin, CabinResult, Job, PriceType, RawAmenity,
                      RawBrand, RawMiles)
from ..normalization import (detect_cabin, effective_cabin, ladder_metrics,
                             regroup_brands_by_cabin)
from ..pricing import parse_price
from ..retry import CarrierAbsent, Forbidden, NoAvailabilityError
from .base import SourceAdapter, register

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
    const headM = (main.innerText || '').match(priceRe);
    const baseText = headM ? headM[0] : '';
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
      const fm = (main.innerText || '').match(/\b([A-Z][A-Z0-9])\s?-?\s?\d{2,4}\b/);
      if (fm) rowCarrier = fm[1].toUpperCase();
    }
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
        lis: lis,
        priceText: dm ? dm[0] : ''
      };
    });
    out.push({ carrier: rowCarrier || (brands[0] && brands[0].iata) || '',
               fare_iata: (brands[0] && brands[0].iata) || '',
               baseText: baseText, brands: brands });
  }
  return out;
}
"""

_BAG_RE = re.compile(r"(cabin baggage|personal item|baggage)\s*:?\s*(\d+)\s*x\s*(\d+)\s*kg", re.I)
_RULE_RE = re.compile(r"^([A-Z][A-Z_]{2,})\s*-\s*(.+)$")
# English "mile(s)" or Turkish "mil" ("%25 Ekstra Mil", "30 PERCENT EXTRA MILES").
_MILES_RE = re.compile(r"\bmil(?:e|es)?\b", re.I)
_PERCENT_RE = re.compile(r"%|percent|yüzde", re.I)


@register
class Ubfly(SourceAdapter):
    name = "Ubfly"
    # Priority 2 (user decision 2026-07-28): Ubfly's per-package rule list is far
    # richer than Enuygun's compact items, so it outranks Enuygun; Enuygun is the
    # last resort and still enriches via the cross-source merge.
    priority = 2
    needs_browser = True

    # Shared across all worker pages: one search per (from,to,date,cabin) serves
    # every carrier on that route.
    _cache: dict[str, list[dict]] = {}
    _locks: dict[str, asyncio.Lock] = {}

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
        """
        rowc = (flight.get("carrier") or "").upper()
        fic = (flight.get("fare_iata") or "").upper()
        if rowc and rowc != target:
            return False
        if fic and fic != target:
            return False
        return rowc == target or fic == target

    def cabins_for(self, job: Job) -> list[Cabin]:
        return [Cabin.ECONOMY, Cabin.BUSINESS]

    def best_buckets(self, carrier_flights: list[dict], search_cabin: Cabin,
                     target: str) -> dict[Cabin, list[RawBrand]]:
        """Pick the best flight's re-bucketed ladder for one searched cabin.

        Most cabins covered (PE presence wins), most fares, fewest implausible
        (>3x) price jumps, smallest worst step, cheapest base — the airline's
        own site sorts cheapest-first. PE side-pick: if the winner lacks PE but
        another carrier flight offers it, that flight contributes the PE bucket.
        Shared by ``fetch_search`` and the screenshot-verification harness so
        both apply the IDENTICAL selection.
        """
        keep_pe = search_cabin == Cabin.ECONOMY
        scored: list[tuple[tuple, dict[Cabin, list[RawBrand]]]] = []
        for f in carrier_flights:
            buckets = regroup_brands_by_cabin(
                self._to_raw_brands(f, search_cabin), search_cabin,
                keep_pe=keep_pe, carrier=target)
            if not buckets:
                continue
            allb = [b for bs in buckets.values() for b in bs]
            bad, worst = ladder_metrics(allb)
            base_val, _, _ = parse_price(_clean_price(f.get("baseText", "")))
            scored.append(((len(buckets), len(allb), -bad, -worst,
                            -(base_val if base_val is not None else float("inf"))), buckets))
        scored.sort(key=lambda t: t[0], reverse=True)
        best = scored[0][1] if scored else {}
        if keep_pe and Cabin.PREMIUM_ECONOMY not in best:
            for _, buckets in scored[1:]:
                pe = buckets.get(Cabin.PREMIUM_ECONOMY)
                if pe:
                    best[Cabin.PREMIUM_ECONOMY] = pe
                    break
        return best

    # ------------------------------------------------------------------ #
    async def fetch_search(self, page, job: Job, departure: date, return_date: date) -> list[CabinResult]:
        results: dict[Cabin, CabinResult] = {}
        target = job.carrier.strip().upper()
        any_flights = False
        for search_cabin in self.cabins_for(job):
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
                if bs and cab not in results:
                    results[cab] = CabinResult(cabin=cab, departure=departure,
                                               return_date=return_date, brands=bs)
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

    async def _do_search(self, page, origin, destination, departure, param) -> list[dict]:
        ddate = departure.strftime("%d.%m.%Y")
        url = (f"{BASE_URL}?from={origin}&to={destination}&ddate={ddate}"
               f"&cabintype={param}&adult=1&flightType=2")
        await page.goto(url, wait_until="domcontentloaded")
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
            return []                                      # genuine "no results"
        await asyncio.sleep(1.0)                            # let the list settle
        try:
            flights = await page.evaluate(_EXTRACT_JS)
        except Exception as e:  # noqa: BLE001
            _LOG.debug("Ubfly extract failed: %s", e)
            return []
        return flights or []

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
            # A box whose own two labels contradict each other (economy-named but
            # structurally tagged Business, e.g. AC "Economy Light"/EL-EL) is
            # untrustworthy junk either way — drop it. PE is exempt: an "economy"
            # tag on a PE fare family is the normal leak pattern, not a conflict.
            name_cab = detect_cabin(name)
            if (name_cab and box_cabin and name_cab != box_cabin
                    and Cabin.PREMIUM_ECONOMY not in (name_cab, box_cabin)):
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
