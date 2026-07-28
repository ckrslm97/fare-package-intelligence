"""Enuygun (OTA aggregator) — last-resort fallback, priority 3.

Enuygun exposes branded fares as **structured JSON** (richer than Ubfly's DOM):
each flight carries ``provider_packages[]`` — a branded-fare ladder where every
package has ``raw_name`` (brand), a delta ``total_price`` (added to the flight's
base fare), and ``items[]`` amenities with a canonical-ish ``name``
(cabin_baggage / checked_baggage / seat_selection / meal / change / refund /
lounge / …), ``is_available`` and a fee hint in the description/tooltip.

Flow (all same-origin, so Cloudflare clearance is shared):
  1. resolve IATA -> airport slug via ``/ucak-bileti/trip-autocomplete/?term=CODE``,
  2. navigate the slug deep-link
     ``/ucak-bileti/arama/<oSlug>-<dSlug>-<orgn>-<dest>/?gidis=DD.MM.YYYY&yetiskin=1&cocuk=0&bebek=0``,
  3. intercept the ``async-result`` JSON and parse it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import date
from typing import Optional

from ..amenities import classify_status_from_text
from ..models import (AmenityStatus, Cabin, CabinResult, Job, PriceType, RawAmenity,
                      RawBrand, RawMiles)
from ..normalization import detect_cabin, ladder_metrics, regroup_brands_by_cabin
from ..retry import CarrierAbsent, Forbidden, NoAvailabilityError
from .base import SourceAdapter, register

_LOG = logging.getLogger("bfs")

HOME = "https://www.enuygun.com/ucak-bileti/"
AUTOCOMPLETE = "/ucak-bileti/trip-autocomplete/?term="

# Enuygun item name -> canonical amenity key (None = outside our taxonomy).
ITEM_MAP = {
    "cabin_baggage": "cabin_baggage",
    "hand_bag": "cabin_baggage",
    "hand_baggage": "cabin_baggage",
    "checked_baggage": "checked_baggage",
    "extra_baggage": "extra_baggage",
    "seat_selection": "seat_selection",
    "seat": "seat_selection",
    "meal": "meal",
    "catering": "meal",
    "lounge": "lounge_access",
    "lounge_access": "lounge_access",
    "priority_boarding": "priority_boarding",
    "priority_security": "fast_track",
    "fast_track": "fast_track",
    "change": "change",
    "reissue": "change",
    "refund": "refund",
    "cancellation": "refund",
    "wifi": "wifi",
    "wi_fi": "wifi",
    "no_show_refund": "no_show_refund",
    "no_show_change": "no_show_change",
    "same_day": "same_day_earlier_flight",
    "pet": "pet",
    "sport": "sports_equipment",
    "sports": "sports_equipment",
    "sports_equipment": "sports_equipment",
    # Deliberately unmapped: priority_check_in, entertainment (outside taxonomy).
}
_MILES_NAMES = {"mile", "miles", "mileage", "mil"}
_seen_unmapped: set[str] = set()   # item names outside ITEM_MAP, logged once each

# Enuygun's search takes a cabin/class query param. Economy is the default (no
# param). The Business / Premium-Economy values are confirmed by a live recon
# before a full run (see recon note) — until a cabin has a non-None value it is
# left disabled so a wrong param can't silently return (and mislabel) the
# default Economy result. ``""`` == enabled with no extra param (Economy).
CLASS_PARAM_NAME = "sinif"
CLASS_PARAM: dict[Cabin, Optional[str]] = {
    Cabin.ECONOMY: "",             # default search (no param)
    Cabin.PREMIUM_ECONOMY: None,   # filled after live recon (if Enuygun exposes it)
    Cabin.BUSINESS: "business",    # confirmed live: sinif=business -> Business ladder
}


@register
class Enuygun(SourceAdapter):
    name = "Enuygun"
    # Priority 3 (user decision 2026-07-28): weak package content -> last resort;
    # its fee tooltips still flow into the data via cross-source enrichment.
    priority = 3
    needs_browser = True

    _slug_cache: dict[str, Optional[str]] = {}
    _slug_lock = asyncio.Lock()
    _result_cache: dict[str, list[dict]] = {}
    _result_locks: dict[str, asyncio.Lock] = {}

    def supports(self, carrier: str) -> bool:
        return True  # aggregator

    def cabins_for(self, job: Job) -> list[Cabin]:
        # Only cabins with a confirmed class param (Economy is the default).
        return [c for c in (Cabin.ECONOMY, Cabin.PREMIUM_ECONOMY, Cabin.BUSINESS)
                if CLASS_PARAM.get(c) is not None]

    # ------------------------------------------------------------------ #
    async def fetch_search(self, page, job: Job, departure: date, return_date: date) -> list[CabinResult]:
        o_slug, d_slug = await self._ensure_slugs(page, job.origin, job.destination)
        if not (o_slug and d_slug):
            raise NoAvailabilityError(f"Enuygun: could not resolve slugs for {job.route}")

        target = job.carrier.strip().upper()
        results: dict[Cabin, CabinResult] = {}
        any_flights = False
        for search_cabin in self.cabins_for(job):
            flights = await self._search(page, job.origin, job.destination,
                                         o_slug, d_slug, departure, search_cabin)
            if flights:
                any_flights = True
            # Prefer itineraries the target carrier markets on *every* segment, so
            # a codeshare (e.g. EK-marketed but AC-fared) can't win the pick and
            # show the partner's brand ladder. Fall back to first-segment match.
            carrier_flights = [f for f in flights if _airline_all(f, target)] \
                or [f for f in flights if _airline(f) == target]
            if not carrier_flights:
                continue

            keep_pe = search_cabin == Cabin.ECONOMY   # PE families leak into the eco ladder
            # Score every carrier flight's ladder AFTER regrouping: most cabins
            # covered, most fares, fewest implausible (>3x) jumps, smallest worst
            # step, most amenity items, cheapest base. Same-brand ladders differ
            # per itinerary (e.g. AC business Latitude step $1,144 on one flight
            # vs $284 on another) — the sane, cheap ladder is what the airline's
            # own site shows first.
            scored = []
            for f in carrier_flights:
                base = _flight_base(f)
                rate = f.get("_usd_rate")
                pkgs = f.get("provider_packages") or []
                if not pkgs:
                    continue
                brands = [self._package_to_brand(pk, order, base, rate, search_cabin)
                          for order, pk in enumerate(pkgs)]
                buckets = regroup_brands_by_cabin(brands, search_cabin,
                                                  keep_pe=keep_pe, carrier=target)
                if not buckets:
                    continue
                allb = [b for bs in buckets.values() for b in bs]
                bad, worst = ladder_metrics(allb)
                n_items = sum(len(b.amenities) for b in allb)
                scored.append(((len(buckets), len(allb), -bad, -worst, n_items,
                                -(base if base is not None else float("inf"))), buckets))
            scored.sort(key=lambda t: t[0], reverse=True)
            best = scored[0][1] if scored else {}
            if keep_pe and Cabin.PREMIUM_ECONOMY not in best:
                for _, buckets in scored[1:]:
                    pe = buckets.get(Cabin.PREMIUM_ECONOMY)
                    if pe:
                        best[Cabin.PREMIUM_ECONOMY] = pe
                        break
            for cab, bs in best.items():
                if bs and cab not in results:
                    results[cab] = CabinResult(cabin=cab, departure=departure,
                                               return_date=return_date, brands=bs)

        if not results:
            if any_flights:
                # Carrier flies the route but Enuygun exposes no branded packages
                # for it -> other dates won't help; fall to the next source.
                raise CarrierAbsent(f"Enuygun: no branded packages for {target} {job.route}")
            raise NoAvailabilityError(f"Enuygun: no flights {job.route} {departure}")
        return list(results.values())

    # ------------------------------------------------------------------ #
    async def _ensure_slugs(self, page, origin: str, destination: str) -> tuple[Optional[str], Optional[str]]:
        if origin in self._slug_cache and destination in self._slug_cache:
            return self._slug_cache[origin], self._slug_cache[destination]
        async with self._slug_lock:
            need = [c for c in (origin, destination) if c not in self._slug_cache]
            if need:
                if not (page.url or "").startswith("https://www.enuygun.com"):
                    await page.goto(HOME, wait_until="domcontentloaded")
                    await self._cookies(page)
                for code in need:
                    slug = await self._resolve_slug(page, code)
                    if slug:                  # don't cache a transient None -> would
                        self._slug_cache[code] = slug   # fail every OND on this airport
        return self._slug_cache.get(origin), self._slug_cache.get(destination)

    async def _resolve_slug(self, page, code: str) -> Optional[str]:
        try:
            data = await page.evaluate(
                """async (code) => {
                    const r = await fetch('/ucak-bileti/trip-autocomplete/?term=' + code,
                        { headers: { 'X-Requested-With': 'XMLHttpRequest' } });
                    return await r.json();
                }""", code)
        except Exception as e:  # noqa: BLE001
            _LOG.debug("Enuygun autocomplete failed for %s: %s", code, e)
            return None
        cu = code.upper()
        best = None
        for e in data or []:
            if str(e.get("id", "")).upper() == cu and not e.get("is_city") and e.get("slug"):
                return e["slug"]
            if best is None and cu in str(e.get("id", "")).upper() and e.get("slug"):
                best = e["slug"]
        return best

    async def _search(self, page, origin, destination, o_slug, d_slug, departure,
                      cabin: Cabin = Cabin.ECONOMY) -> list[dict]:
        param = CLASS_PARAM.get(cabin) or ""
        # The class param MUST be part of the cache key, else the cached Economy
        # JSON would be served to the Business / PE search.
        key = f"{origin}|{destination}|{departure.isoformat()}|{param or 'eco'}"
        if key in self._result_cache:
            return self._result_cache[key]
        lock = self._result_locks.setdefault(key, asyncio.Lock())
        async with lock:
            if key in self._result_cache:
                return self._result_cache[key]
            flights = await self._do_search(page, o_slug, d_slug, origin, destination,
                                            departure, param)
            if flights:                       # don't cache transient empty/timeout
                self._result_cache[key] = flights
            return flights

    async def _do_search(self, page, o_slug, d_slug, orgn, dest, departure, param="") -> list[dict]:
        bodies: list[str] = []

        async def on_resp(resp):
            try:
                if "async-result" in resp.url and resp.status == 200:
                    t = await resp.text()
                    if len(t) > 5000:
                        bodies.append(t)
            except Exception:  # noqa: BLE001
                pass

        page.on("response", on_resp)
        try:
            ddate = departure.strftime("%d.%m.%Y")
            url = (f"https://www.enuygun.com/ucak-bileti/arama/"
                   f"{o_slug}-{d_slug}-{orgn.lower()}-{dest.lower()}/"
                   f"?gidis={ddate}&yetiskin=1&cocuk=0&bebek=0")
            if param:
                url += f"&{CLASS_PARAM_NAME}={param}"
            await page.goto(url, wait_until="domcontentloaded")
            await self._cookies(page)
            title = (await page.title()) or ""
            if re.search(r"just a moment|attention required|access denied", title, re.I):
                raise Forbidden("Enuygun Cloudflare challenge")
            # Poll for the async-result body (Enuygun streams partial -> full).
            waited = 0.0
            while waited < 40.0:
                await asyncio.sleep(0.5)
                waited += 0.5
                if bodies and waited >= 4.0:
                    break
        finally:
            page.remove_listener("response", on_resp)

        if not bodies:
            return []
        best = max(bodies, key=len)
        try:
            data = json.loads(best)
        except Exception:  # noqa: BLE001
            return []
        flights = (data.get("flights") or {}).get("departure") or []
        # Enuygun prices are TRY; carry the site's own TRY->USD rate so we can
        # report in USD (matching Ubfly) for a uniform dataset.
        rate = (((data.get("currency_rates") or {}).get("TRY") or {}).get("Rates") or {}).get("USD")
        for f in flights:
            f["_usd_rate"] = rate
        return flights

    async def _cookies(self, page) -> None:
        for pat in (r"KABUL ET", r"Onayla", r"Accept"):
            try:
                btn = page.get_by_role("button", name=re.compile(pat, re.I))
                if await btn.count():
                    await btn.first.click(timeout=3000)
                    return
            except Exception:
                continue

    # ------------------------------------------------------------------ #
    def _package_to_brand(self, pk: dict, order: int, base: Optional[float],
                          usd_rate: Optional[float] = None,
                          searched_cabin: Cabin = Cabin.ECONOMY) -> RawBrand:
        raw_name = pk.get("raw_name") or pk.get("name") or f"Fare {order + 1}"
        # Default to the cabin we actually searched (not always Economy), so a
        # Business/PE package whose name carries no cabin word is bucketed right.
        cabin = detect_cabin(raw_name) or searched_cabin
        delta = pk.get("total_price")
        if delta is None:
            delta = pk.get("price")
        try:
            delta = float(delta) if delta is not None else None
        except (TypeError, ValueError):
            delta = None
        absolute = None
        if base is not None and delta is not None:
            absolute = round(base + delta, 2)
        elif delta is not None:
            absolute = delta
        # Convert TRY -> USD (site's own rate) so the dataset is uniformly USD.
        currency = pk.get("currency") or "TRY"
        if usd_rate and absolute is not None and currency.upper() == "TRY":
            absolute = round(absolute * usd_rate, 2)
            if delta is not None:
                delta = round(delta * usd_rate, 2)
            currency = "USD"

        amenities: list[RawAmenity] = []
        miles = RawMiles()
        desc_bits: list[str] = []
        for it in pk.get("items") or []:
            name = (it.get("name") or "").lower()
            at = it.get("attributes") or {}
            descr = it.get("description") or at.get("display_text") or ""
            tooltip = (at.get("tooltip") or {}).get("text", "") or ""
            if name in _MILES_NAMES:
                miles.mileage_available = bool(at.get("is_available", True))
                continue
            key = ITEM_MAP.get(name)
            if not key:
                if name and name not in _seen_unmapped:   # keep taxonomy gaps visible
                    _seen_unmapped.add(name)
                    _LOG.debug("Enuygun unmapped item name: %r (desc=%r)", name, descr[:60])
                if descr:
                    desc_bits.append(f"{name}: {descr}")
                continue
            status = self._item_status(at, descr)
            detail = descr
            # Surface the fee amount from the tooltip ("Yakl. 10759.00 TRY …")
            # so Paid cells carry the actual cost.
            if tooltip and any(ch.isdigit() for ch in tooltip) and tooltip not in detail:
                detail = f"{descr} — {tooltip}" if descr else tooltip
            amenities.append(RawAmenity(raw_label=name, status=status, raw_value=detail,
                                        canonical_key=key))

        return RawBrand(
            raw_brand_name=raw_name, cabin=cabin, screen_order=order,
            price_value=absolute, price_type=PriceType.ABSOLUTE, currency=currency,
            display_price_text=(f"+{delta:g} {currency}" if delta else f"0 {currency}"),
            fare_family_code=pk.get("automation_key") or pk.get("id"),
            description="; ".join(desc_bits), amenities=amenities, miles=miles)

    @staticmethod
    def _item_status(at: dict, descr: str) -> AmenityStatus:
        if at.get("is_available") is False:
            return AmenityStatus.NOT_INCLUDED
        tooltip = (at.get("tooltip") or {}).get("text", "")
        text = " ".join([descr or "", at.get("display_text") or "", tooltip])
        st = classify_status_from_text(text)
        if st == AmenityStatus.PAID or "kesintili" in text.lower():
            return AmenityStatus.PAID
        if st == AmenityStatus.NOT_INCLUDED:      # textual negative must not be
            return AmenityStatus.NOT_INCLUDED     # overridden by the Included default
        return AmenityStatus.INCLUDED


def _airline(flight: dict) -> str:
    seg = (flight.get("segments") or [{}])[0]
    for k in ("marketing_airline", "airline", "operating_airline", "carrier"):
        v = seg.get(k)
        if v:
            return str(v).upper()
    return ""


def _seg_marketing(seg: dict) -> str:
    for k in ("marketing_airline", "airline", "carrier"):
        v = seg.get(k)
        if v:
            return str(v).upper()
    return ""


def _airline_all(flight: dict, target: str) -> bool:
    """True only if the target carrier markets *every* segment — so a codeshare
    (e.g. an EK-marketed itinerary flown/fared by AC) can't pass as the target
    and contribute the partner's branded ladder."""
    segs = flight.get("segments") or []
    marketing = [_seg_marketing(s) for s in segs]
    return bool(marketing) and all(m == target for m in marketing)


def _flight_base(flight: dict) -> Optional[float]:
    price = flight.get("price") or {}
    for k in ("total", "base"):
        v = price.get(k)
        if isinstance(v, (int, float)) and v:
            return float(v)
    return None
