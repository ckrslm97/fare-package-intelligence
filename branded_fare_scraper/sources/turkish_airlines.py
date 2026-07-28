"""Turkish Airlines (TK) — official-site adapter. Priority 1.

Strategy (see memory ``thy-availability-api``): drive the public booking form
just far enough to trigger the site's own availability call, then read the JSON
straight off the wire (``POST /api/v1/availability``). That JSON carries the
authoritative branded-fare data — absolute prices, currency, the full rights
matrix, fare-family (brand) codes and bonus-mile percentage — for every cabin in
a single response. Brand *display names* and *miles earned* are not in that JSON,
so they are read from the rendered comparison table and joined back to the API
brands by exact price.

This avoids replaying CSRF/session/signed requests and is far more stable than
scraping the rendered fare cards for every field.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import date
from typing import Any, Optional

from ..models import (
    AmenityStatus, Cabin, CabinResult, Job, PriceType, RawAmenity, RawBrand, RawMiles,
)
from ..retry import Forbidden, NoAvailabilityError, PermanentError, TimeoutErr
from .base import SourceAdapter, register

_LOG = logging.getLogger("bfs")

BOOKING_URL = "https://www.turkishairlines.com/en-int/flights/booking/"
AVAILABILITY_PATH = "/api/v1/availability"

_CABIN_MAP = {
    "ECONOMY": Cabin.ECONOMY,
    "PREMIUM_ECONOMY": Cabin.PREMIUM_ECONOMY,
    "BUSINESS": Cabin.BUSINESS,
    "FIRST": Cabin.FIRST,
}

# THY brandRight rightType -> canonical amenity key. Mile rights are handled
# separately (as miles), so they map to None here.
_RIGHT_MAP: dict[str, Optional[str]] = {
    "CARRY_ON_BAGGAGE": "cabin_baggage",
    "CHECKED_BAGGAGE": "checked_baggage",
    "EXTRA_BAGGAGE": "extra_baggage",
    "MERGED_SEAT_SELECTION": "seat_selection",
    "SEAT_SELECTION": "seat_selection",
    "PAID_SEAT_SELECTION": "seat_selection",
    "REISSUE": "change",
    "RESERVATION_CHANGE": "change",
    "CHANGE": "change",
    "SAME_DAY_CHANGE": "same_day_earlier_flight",
    "EARLIER_FLIGHT_SAME_DAY": "same_day_earlier_flight",
    "REFUND": "refund",
    "FAST_TRACK": "fast_track",
    "LOUNGE": "lounge_access",
    "LOUNGE_USAGE": "lounge_access",
    "MEAL": "meal",
    "CATERING": "meal",
    "WIFI": "wifi",
    "PRIORITY_BOARDING": "priority_boarding",
    # Rights outside the target taxonomy are recorded in the description only.
    "FULL_REFUND_IN_24_HOURS": None,
    "CANCELLATION": None,
    "NO_SHOW": None,
    "STANDBY": None,
    "DEDICATED_CHECKIN": None,
    "UPGRADE": None,
    "BONUS_MILE": None,
    "MILE": None,
    "BASE_MILE": None,
}

# Fallback display names by brand code (used only if the DOM read fails).
_BRAND_NAME_FALLBACK = {
    "CL": "EcoFly", "LG": "ExtraFly", "GN": "FlexFly",
}

# JS that reads the currently-expanded fare-comparison panel and returns, in
# left-to-right column order, each brand's display name, miles-earned and price.
_DOM_EXTRACT_JS = r"""
() => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  // Find a container that holds the branded comparison (has 'Package details').
  let panel = null;
  const all = Array.from(document.querySelectorAll('div,section'));
  for (const el of all) {
    const t = el.innerText || '';
    if (/Package details/i.test(t) && /Miles/i.test(t) && t.length < 8000) { panel = el; break; }
  }
  if (!panel) return [];
  const text = panel.innerText || '';
  // Brand columns end with "#### Miles" then a price "16,306 .99 TL".
  // Split into blocks on brand-name lines that precede "O Class"/"Class".
  const cols = [];
  const re = /([A-Za-zÇĞİÖŞÜçğıöşü&\-\. ]{3,30}?)\n[A-Z]?\s?Class[\s\S]*?(\d[\d.,]*)\s*Miles[\s\S]*?([\d.,]+)\s*\.(\d{2})\s*TL/g;
  let m;
  while ((m = re.exec(text)) !== null) {
    const name = norm(m[1]).split('\n').pop();
    const miles = parseFloat(m[2].replace(/[.,]/g, ''));
    const price = parseFloat((m[3] + '.' + m[4]).replace(/,/g, ''));
    cols.push({ name, miles, price });
  }
  return cols;
}
"""


@register
class TurkishAirlines(SourceAdapter):
    name = "Turkish Airlines"
    priority = 1
    needs_browser = True

    def supports(self, carrier: str) -> bool:
        c = (carrier or "").strip().lower()
        return c in {"tk", "thy", "turkish", "turkish airlines", "turkishairlines",
                     "türk hava yolları", "turk hava yollari"}

    # ------------------------------------------------------------------ #
    async def fetch_search(self, page, job: Job, departure: date, return_date: date) -> list[CabinResult]:
        captured: dict[str, Any] = {}

        async def on_response(resp):
            try:
                if AVAILABILITY_PATH in resp.url and resp.request.method == "POST" and resp.status == 200:
                    captured["data"] = await resp.json()
            except Exception:  # noqa: BLE001
                pass

        page.on("response", on_response)
        try:
            await self._open_and_search(page, job, departure, return_date)
            data = await self._await_data_or_wall(page, captured)
        finally:
            page.remove_listener("response", on_response)

        cabins = self._parse_availability(data, departure, return_date)
        if not cabins or not any(c.brands for c in cabins):
            raise NoAvailabilityError(f"TK no branded fares for {job.route} {departure}")

        # Best-effort enrichment: brand display names + miles earned from the DOM.
        try:
            await self._enrich_from_dom(page, cabins)
        except Exception as e:  # noqa: BLE001
            _LOG.debug("TK DOM enrichment skipped: %s", e)
        return cabins

    # ------------------------------------------------------------------ #
    async def _open_and_search(self, page, job: Job, departure: date, return_date: date) -> None:
        # "commit" returns as soon as navigation commits — we then wait for the
        # specific form element rather than the whole heavy SPA "load" event.
        await page.goto(BOOKING_URL, wait_until="commit")
        try:
            await page.get_by_role("combobox", name=re.compile(r"^From$", re.I)).wait_for(
                state="visible", timeout=self._t(page))
        except Exception as e:
            raise TimeoutErr(f"TK booking form did not render: {e}") from e
        await self._accept_cookies(page)

        # Detect a bot-wall / access-denied interstitial early.
        title = (await page.title()) or ""
        if re.search(r"access denied|forbidden|blocked", title, re.I):
            raise Forbidden("TK access denied (bot wall)")

        await self._fill_airport(page, "From", job.origin)
        await self._fill_airport(page, "To", job.destination)
        await self._select_dates(page, departure, return_date)

        # Submit.
        search = page.get_by_role("button", name=re.compile(r"Search flights", re.I))
        try:
            await search.click(timeout=self._t(page))
        except Exception:
            # Calendar overlay may intercept; confirm with OK then retry.
            with_ok = page.get_by_role("button", name=re.compile(r"^OK$", re.I))
            if await with_ok.count():
                await with_ok.first.click()
            await search.click(timeout=self._t(page))

    async def _accept_cookies(self, page) -> None:
        for pat in (r"only necessary", r"accept all"):
            btn = page.get_by_role("button", name=re.compile(pat, re.I))
            try:
                if await btn.count():
                    await btn.first.click(timeout=4000)
                    return
            except Exception:
                continue

    async def _fill_airport(self, page, field: str, code: str) -> None:
        box = page.get_by_role("combobox", name=re.compile(rf"^{field}$", re.I))
        await box.click(timeout=self._t(page))
        await box.fill("")
        await box.type(code, delay=70)
        # The locations API is async: a "See all destinations" placeholder shows
        # first and the real airport options (e.g. "…Airport(IST)…") arrive ~2-3s
        # later. Poll until the option that actually matches the IATA code appears.
        needle = f"({code.upper()})"
        target = None
        for _ in range(30):                       # up to ~12s
            options = page.get_by_role("option")
            n = await options.count()
            for i in range(n):
                try:
                    txt = (await options.nth(i).inner_text()).upper()
                except Exception:
                    continue
                if needle in txt:
                    target = options.nth(i)
                    break
            if target:
                break
            await asyncio.sleep(0.4)
        if target is None:
            raise TimeoutErr(f"TK autocomplete: no option matching {code} for {field}")
        await target.click()
        await asyncio.sleep(0.3)                    # let the field commit

    async def _select_dates(self, page, departure: date, return_date: date) -> None:
        await self._click_calendar_day(page, departure)
        await self._click_calendar_day(page, return_date)

    async def _click_calendar_day(self, page, target: date) -> None:
        month = target.strftime("%B")
        sel = (f'button.react-calendar__tile:has(abbr[aria-label^="{month} {target.day} "]'
               f'[aria-label*="{target.year}"])')
        next_btn = page.get_by_role("button", name=re.compile(r"next month", re.I))
        for _ in range(24):
            tile = page.locator(sel)
            if await tile.count():
                el = tile.first
                if await el.is_enabled():
                    await el.click()
                    return
            if await next_btn.count():
                await next_btn.first.click()
                await asyncio.sleep(0.15)
            else:
                break
        raise NoAvailabilityError(f"TK date {target} not selectable")

    async def _await_data_or_wall(self, page, captured: dict, timeout_s: float = 30.0) -> dict:
        """Return the availability JSON, or bail fast if a bot-wall appears.

        Turkish Airlines fronts the booking flow with PerimeterX ("Press & Hold").
        Detecting it early lets the runner fall back to the OTA tier in seconds
        instead of burning three 30s retries.
        """
        waited = 0.0
        while "data" not in captured and waited < timeout_s:
            if waited >= 1.0 and int(waited * 4) % 8 == 0 and await self._bot_wall(page):
                raise PermanentError("TK PerimeterX bot-wall (press & hold) — falling back")
            await asyncio.sleep(0.25)
            waited += 0.25
        if "data" not in captured:
            if await self._bot_wall(page):
                raise PermanentError("TK PerimeterX bot-wall — falling back")
            raise TimeoutErr("TK availability response not captured")
        return captured["data"]

    async def _bot_wall(self, page) -> bool:
        try:
            return bool(await page.evaluate(
                """() => /press ?& ?hold|before we continue|verify you are a human/i.test(document.body.innerText||'')
                        || !!document.querySelector('[id*="px-captcha"], iframe[src*="px-cdn"], iframe[src*="captcha-delivery"]')"""))
        except Exception:
            return False

    # ------------------------------------------------------------------ #
    def _parse_availability(self, data: dict, departure: date, return_date: date) -> list[CabinResult]:
        root = (data or {}).get("data", data) or {}
        od_list = root.get("originDestinationInformationList") or []
        if not od_list:
            return []
        outbound = od_list[0]
        if outbound.get("soldOutAllFlights"):
            return []
        options = outbound.get("originDestinationOptionList") or []

        # For each cabin, pick the flight option exposing the most brands (fullest ladder).
        best_by_cabin: dict[Cabin, list[dict]] = {}
        for opt in options:
            fare_cat = opt.get("fareCategory") or {}
            for cabin_key, cat in fare_cat.items():
                cabin = _CABIN_MAP.get(cabin_key)
                if not cabin:
                    continue
                brands = cat.get("bookingPriceInfoList") or []
                if len(brands) > len(best_by_cabin.get(cabin, [])):
                    best_by_cabin[cabin] = brands

        results: list[CabinResult] = []
        for cabin, brand_infos in best_by_cabin.items():
            raw_brands: list[RawBrand] = []
            for order, bi in enumerate(brand_infos):
                raw_brands.append(self._brand_from_info(bi, cabin, order))
            results.append(CabinResult(
                cabin=cabin, departure=departure, return_date=return_date,
                brands=raw_brands, has_availability=bool(raw_brands),
            ))
        return results

    def _brand_from_info(self, bi: dict, cabin: Cabin, order: int) -> RawBrand:
        gtf = bi.get("grandTotalFare") or {}
        amount = gtf.get("amount")
        currency = gtf.get("currencyCode")
        sign = gtf.get("currencySign") or currency or ""
        brand_code = bi.get("brandCode") or (bi.get("brandCodeList") or [None])[0]

        amenities: list[RawAmenity] = []
        miles = RawMiles()
        desc_bits: list[str] = []
        for right in bi.get("brandRightList") or []:
            rtype = right.get("rightType", "")
            if rtype in ("BONUS_MILE", "MILE", "BASE_MILE"):
                args = right.get("args") or {}
                miles.mileage_available = bool(right.get("hasRight"))
                pct = args.get("percentage")
                if pct is not None:
                    try:
                        miles.bonus_percent = float(pct)
                    except (TypeError, ValueError):
                        pass
                continue
            key = _RIGHT_MAP.get(rtype)
            status, raw_value = self._right_status(right)
            if key is None:
                # Unmapped right: still record for description context.
                if raw_value:
                    desc_bits.append(f"{rtype}: {raw_value}")
                continue
            amenities.append(RawAmenity(raw_label=rtype, status=status, raw_value=raw_value,
                                        fee_amount=self._fee_amount(right),
                                        fee_currency=(right.get("args") or {}).get("currency"),
                                        canonical_key=key))
            if raw_value and key in ("checked_baggage", "cabin_baggage"):
                desc_bits.append(f"{key.replace('_', ' ')}: {raw_value}")

        return RawBrand(
            raw_brand_name=_BRAND_NAME_FALLBACK.get(brand_code, brand_code or f"{cabin.value} #{order}"),
            cabin=cabin,
            screen_order=order,
            price_value=float(amount) if amount is not None else None,
            price_type=PriceType.ABSOLUTE,
            currency=currency,
            display_price_text=f"{amount} {sign}".strip() if amount is not None else "",
            fare_family_code=brand_code,
            description="; ".join(desc_bits),
            amenities=amenities,
            miles=miles,
        )

    @staticmethod
    def _right_status(right: dict) -> tuple[AmenityStatus, str]:
        rs = right.get("rightStatus", "") or ""
        has = right.get("hasRight")
        charge = right.get("withCharge")
        args = right.get("args") or {}
        # Build a human raw value from args.
        parts = []
        if args.get("kilo"):
            parts.append(f"{args['kilo']} KG")
        if args.get("piece"):
            parts.append(f"{args['piece']} PC")
        if args.get("amount"):
            parts.append(f"{args['amount']} {args.get('currency', '')}".strip())
        if args.get("percentage"):
            parts.append(f"{args['percentage']}%")
        raw_value = " x ".join(parts) if parts else rs

        if has is False or "NOT_ALLOWED" in rs:
            return AmenityStatus.NOT_INCLUDED, raw_value
        if charge is True or "PENALTY" in rs:
            return AmenityStatus.PAID, raw_value
        if has is True or "ALLOWED" in rs:
            return AmenityStatus.INCLUDED, raw_value
        return AmenityStatus.UNKNOWN, raw_value

    @staticmethod
    def _fee_amount(right: dict) -> Optional[float]:
        args = right.get("args") or {}
        try:
            return float(args["amount"]) if args.get("amount") is not None else None
        except (TypeError, ValueError):
            return None

    # ------------------------------------------------------------------ #
    async def _enrich_from_dom(self, page, cabins: list[CabinResult]) -> None:
        """Expand each cabin's comparison and attach brand names + miles by price."""
        expanders = page.get_by_role(
            "button", name=re.compile(r"cabin\. Best available fare", re.I))
        count = await expanders.count()
        price_map: dict[int, dict] = {}
        for i in range(min(count, 6)):
            try:
                await expanders.nth(i).click(timeout=5000)
                await asyncio.sleep(0.4)
                cols = await page.evaluate(_DOM_EXTRACT_JS)
            except Exception:
                continue
            for col in cols or []:
                price = col.get("price")
                if price is None:
                    continue
                price_map[round(float(price))] = {"name": col.get("name"), "miles": col.get("miles")}

        if not price_map:
            return
        for cab in cabins:
            for b in cab.brands:
                if b.price_value is None:
                    continue
                hit = price_map.get(round(b.price_value))
                if not hit:
                    continue
                if hit.get("name"):
                    b.raw_brand_name = hit["name"]
                if hit.get("miles") is not None:
                    b.miles.miles_earned = float(hit["miles"])
                    if b.miles.mileage_available is None:
                        b.miles.mileage_available = True

    @staticmethod
    def _t(page) -> int:
        return 20000
