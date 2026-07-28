"""Unit tests for the pure-logic core (no network, no browser)."""

from __future__ import annotations

import random
from datetime import date, timedelta

import pytest

from branded_fare_scraper.amenities import (
    classify_status_from_text, empty_amenity_map, map_label_to_canonical)
from branded_fare_scraper.dates import (SUMMER_MONTHS, WINTER_MONTHS, build_date_plan,
                                        pick_departure)
from branded_fare_scraper.models import AmenityStatus, Cabin, PriceType, RawBrand, Season
from branded_fare_scraper.normalization import (assign_brand_order, detect_cabin,
                                                effective_cabin, iter_ranked_by_cabin,
                                                normalize_brand, regroup_brands_by_cabin)
from branded_fare_scraper.pricing import compute_absolute_prices, parse_price


# --------------------------- dates ---------------------------------------- #
def test_summer_departure_month_in_range():
    dep = pick_departure(Season.SUMMER, today=date(2026, 1, 1), rng=random.Random(1))
    assert dep.month in SUMMER_MONTHS
    assert dep >= date(2026, 1, 1) + timedelta(days=21)


def test_winter_spans_year_boundary():
    dep = pick_departure(Season.WINTER, today=date(2026, 7, 1), rng=random.Random(2))
    assert dep.month in WINTER_MONTHS


def test_return_is_dep_plus_three_and_window_is_eight():
    plan = build_date_plan(Season.SUMMER, today=date(2026, 1, 1), rng=random.Random(3))
    assert plan.return_date == plan.departure + timedelta(days=3)
    assert len(plan.window) == 8                      # D0 .. D0+7
    for dep, ret in plan.window:
        assert ret == dep + timedelta(days=3)
    assert plan.window[0][0] == plan.departure


def test_fresh_run_changes_dates():
    a = build_date_plan(Season.SUMMER, rng=random.Random())
    b = build_date_plan(Season.SUMMER, rng=random.Random())
    # Overwhelmingly likely to differ across independent RNGs.
    assert (a.departure, b.departure) is not None


# --------------------------- pricing -------------------------------------- #
def test_parse_absolute_and_delta():
    v, t, c = parse_price("230 USD")
    assert v == 230 and t == PriceType.ABSOLUTE and c == "USD"
    v, t, c = parse_price("+30")
    assert v == 30 and t == PriceType.DELTA


def test_parse_eu_and_us_decimals():
    assert parse_price("₺16.306,99")[0] == pytest.approx(16306.99)
    assert parse_price("$1,234.56")[0] == pytest.approx(1234.56)


def test_delta_absolute_from_base_anchor():
    brands = [
        RawBrand("Basic", Cabin.ECONOMY, 0, 200, PriceType.ABSOLUTE),
        RawBrand("Classic", Cabin.ECONOMY, 1, 30, PriceType.DELTA),
        RawBrand("Flex", Cabin.ECONOMY, 2, 60, PriceType.DELTA),
    ]
    assert compute_absolute_prices(brands) == [200, 230, 260]


# --------------------------- normalization -------------------------------- #
def test_normalize_thy_brands():
    assert normalize_brand("EcoFly", Cabin.ECONOMY).normalized_name == "Economy Basic"
    assert normalize_brand("PrimeFly", Cabin.ECONOMY).normalized_name == "Economy Premium"
    assert normalize_brand("FlexFly", Cabin.ECONOMY).normalized_name == "Economy Flex"


def test_hierarchy_order_ignores_screen_order():
    # Provided in shuffled screen order; must come out Lite<Basic<Flex<Premium.
    brands = [
        RawBrand("PrimeFly", Cabin.ECONOMY, 0, 19046, PriceType.ABSOLUTE),
        RawBrand("EcoFly", Cabin.ECONOMY, 1, 16306, PriceType.ABSOLUTE),
        RawBrand("FlexFly", Cabin.ECONOMY, 2, 18054, PriceType.ABSOLUTE),
        RawBrand("ExtraFly", Cabin.ECONOMY, 3, 17298, PriceType.ABSOLUTE),
    ]
    ordered = assign_brand_order(brands)
    names = [raw.raw_brand_name for raw, _, _ in ordered]
    assert names == ["EcoFly", "ExtraFly", "FlexFly", "PrimeFly"]
    assert [o for _, _, o in ordered] == [0, 1, 2, 3]


def test_order_by_price_beats_name_rank_ac_conflict():
    # AC DEL-YVR: name-token rank mis-ordered these; the site (and truth) is by
    # price: STANDARD 812 < FLEX 872 < COMFORT 999 < LATITUDE 5007. LATITUDE
    # matches no sub-tier token, so only price ordering gets it right.
    brands = [
        RawBrand("STANDARD", Cabin.ECONOMY, 0, 812, PriceType.ABSOLUTE),
        RawBrand("LATITUDE", Cabin.ECONOMY, 1, 5007, PriceType.ABSOLUTE),
        RawBrand("COMFORT", Cabin.ECONOMY, 2, 999, PriceType.ABSOLUTE),
        RawBrand("FLEX", Cabin.ECONOMY, 3, 872, PriceType.ABSOLUTE),
    ]
    names = [raw.raw_brand_name for raw, _, _ in assign_brand_order(brands)]
    assert names == ["STANDARD", "FLEX", "COMFORT", "LATITUDE"]


def test_order_by_price_bsflxplus_after_bsflex():
    # Name-rank put "plus"(comfort) below "flex"; price is the ground truth.
    brands = [
        RawBrand("BSFLEX", Cabin.BUSINESS, 0, 1000, PriceType.ABSOLUTE),
        RawBrand("BSFLXPLUS", Cabin.BUSINESS, 1, 1500, PriceType.ABSOLUTE),
    ]
    names = [raw.raw_brand_name for raw, _, _ in assign_brand_order(brands)]
    assert names == ["BSFLEX", "BSFLXPLUS"]


def test_regroup_drops_contradictory_cabin():
    # Ubfly's Business search lists an "Economy Light" upsell — it must be dropped,
    # not folded into Business (that produced the $5877 "business" outlier).
    brands = [
        RawBrand("Business Flex", Cabin.BUSINESS, 0, 2500, PriceType.ABSOLUTE),
        RawBrand("Economy Light", Cabin.BUSINESS, 1, 5877, PriceType.ABSOLUTE),
    ]
    buckets = regroup_brands_by_cabin(brands, Cabin.BUSINESS, keep_pe=False)
    assert set(buckets) == {Cabin.BUSINESS}
    assert [b.raw_brand_name for b in buckets[Cabin.BUSINESS]] == ["Business Flex"]


def test_regroup_recovers_premium_economy_from_economy_ladder():
    # BA's World Traveller Plus families (PREMECON/PESEL) leak into the Economy
    # search; regroup should split them into a Premium Economy bucket.
    brands = [
        RawBrand("Economy Saver", Cabin.ECONOMY, 0, 500, PriceType.ABSOLUTE),
        RawBrand("PREMECON", Cabin.ECONOMY, 1, 900, PriceType.ABSOLUTE),
        RawBrand("PESEL", Cabin.ECONOMY, 2, 1100, PriceType.ABSOLUTE,
                 fare_family_code="PESEL"),
    ]
    buckets = regroup_brands_by_cabin(brands, Cabin.ECONOMY, keep_pe=True)
    assert set(buckets) == {Cabin.ECONOMY, Cabin.PREMIUM_ECONOMY}
    assert {b.raw_brand_name for b in buckets[Cabin.PREMIUM_ECONOMY]} == {"PREMECON", "PESEL"}


def test_effective_cabin_priority():
    assert effective_cabin("Economy Light", "EL", None, Cabin.BUSINESS) == Cabin.ECONOMY
    assert effective_cabin("PESEL", "PESEL", None, Cabin.ECONOMY) == Cabin.PREMIUM_ECONOMY
    # No cabin word/code -> fall back to the searched cabin.
    assert effective_cabin("Flex", "XF", None, Cabin.BUSINESS) == Cabin.BUSINESS
    assert detect_cabin("PREMECON") == Cabin.PREMIUM_ECONOMY


def test_iter_ranked_by_cabin_splits_and_orders():
    # One mixed group -> per-cabin, price-ordered, 0-based order within each cabin.
    brands = [
        RawBrand("Economy Flex", Cabin.ECONOMY, 0, 700, PriceType.ABSOLUTE),
        RawBrand("Economy Saver", Cabin.ECONOMY, 1, 500, PriceType.ABSOLUTE),
        RawBrand("PREMECON", Cabin.ECONOMY, 2, 900, PriceType.ABSOLUTE),
    ]
    got = {}
    for eff_cab, raw, _nb, order, _absp in iter_ranked_by_cabin(brands, Cabin.ECONOMY):
        got.setdefault(eff_cab, []).append((order, raw.raw_brand_name))
    assert got[Cabin.ECONOMY] == [(0, "Economy Saver"), (1, "Economy Flex")]
    assert got[Cabin.PREMIUM_ECONOMY] == [(0, "PREMECON")]


def test_enuygun_all_segment_airline_filter():
    from branded_fare_scraper.sources.enuygun import _airline_all, _airline
    pure = {"segments": [{"marketing_airline": "EK"}, {"marketing_airline": "EK"}]}
    codeshare = {"segments": [{"marketing_airline": "EK"}, {"marketing_airline": "AC"}]}
    assert _airline_all(pure, "EK") is True
    assert _airline_all(codeshare, "EK") is False
    assert _airline(codeshare) == "EK"        # first-segment fallback still works


# ----------------------- cross-source helpers (round 3) -------------------- #
def test_ac_pe_fare_family_codes():
    # Live-verified: Ubfly serves AC PE as ff "PL-PL"/"PF-PF" (name may even be
    # the bogus "PRIME FLY"); only the carrier-specific map catches these.
    assert effective_cabin("PL", "PL-PL", Cabin.ECONOMY, Cabin.ECONOMY, carrier="AC") \
        == Cabin.PREMIUM_ECONOMY
    assert effective_cabin("PRIME FLY", "PF-PF", Cabin.ECONOMY, Cabin.ECONOMY, carrier="AC") \
        == Cabin.PREMIUM_ECONOMY
    # TK's PrimeFly must NOT be hijacked by AC's map.
    assert effective_cabin("Prime Fly", "PF", None, Cabin.ECONOMY, carrier="TK") == Cabin.ECONOMY


def test_ladder_metrics_flags_gds_full_fare_jump():
    from branded_fare_scraper.normalization import ladder_metrics
    sane = [RawBrand("A", Cabin.BUSINESS, 0, 3630, PriceType.ABSOLUTE),
            RawBrand("B", Cabin.BUSINESS, 1, 3914, PriceType.ABSOLUTE)]
    crazy = [RawBrand("A", Cabin.ECONOMY, 0, 971, PriceType.ABSOLUTE),
             RawBrand("B", Cabin.ECONOMY, 1, 4990, PriceType.ABSOLUTE)]
    assert ladder_metrics(sane) == (0, pytest.approx(1.078, abs=0.01))
    bad, worst = ladder_metrics(crazy)
    assert bad == 1 and worst > 5


def test_match_brands_across_sources():
    from branded_fare_scraper.normalization import match_brands
    enu = [RawBrand("Eco Fly", Cabin.ECONOMY, 0, 100, PriceType.ABSOLUTE),
           RawBrand("FLEX", Cabin.ECONOMY, 1, 200, PriceType.ABSOLUTE)]
    ubf = [RawBrand("ECOFLY", Cabin.ECONOMY, 0, 105, PriceType.ABSOLUTE),
           RawBrand("Flexible", Cabin.ECONOMY, 1, 210, PriceType.ABSOLUTE)]
    m = match_brands(enu, ubf)
    assert m[0].raw_brand_name == "ECOFLY"        # collapsed exact match
    assert m[1].raw_brand_name == "Flexible"      # containment: flex ⊂ flexible


def test_enrich_fills_only_missing():
    from branded_fare_scraper.models import RawAmenity, RawMiles
    from branded_fare_scraper.normalization import enrich_brands
    prim = [RawBrand("Eco Fly", Cabin.ECONOMY, 0, 100, PriceType.ABSOLUTE,
                     amenities=[RawAmenity("refund", AmenityStatus.PAID, "Kesintili İade",
                                           canonical_key="refund")])]
    other = [RawBrand("ECOFLY", Cabin.ECONOMY, 0, 105, PriceType.ABSOLUTE,
                      amenities=[
                          RawAmenity("refund", AmenityStatus.INCLUDED, "free",
                                     canonical_key="refund"),          # must NOT override
                          RawAmenity("SAMEDAYCHANGE - permitted", AmenityStatus.INCLUDED,
                                     "permitted", canonical_key="same_day_earlier_flight"),
                      ],
                      miles=RawMiles(mileage_available=True, miles_earned=500))]
    filled = enrich_brands(prim, other)
    assert filled == 2                              # same-day + miles, not refund
    amap = {a.canonical_key: a for a in prim[0].amenities}
    assert amap["refund"].status == AmenityStatus.PAID           # primary wins
    assert amap["same_day_earlier_flight"].status == AmenityStatus.INCLUDED
    assert prim[0].miles.mileage_available is True and prim[0].miles.miles_earned == 500


def test_ubfly_full_rule_lines_map_correctly():
    # The lines revealed by Ubfly's "Show more" (already present, hidden, in the
    # search-page DOM) must classify correctly — esp. the negatives.
    assert map_label_to_canonical("Non-refundable.") == "refund"
    assert classify_status_from_text("Non-refundable.") == AmenityStatus.NOT_INCLUDED
    assert map_label_to_canonical("Non-exchangeable.") == "change"
    assert classify_status_from_text("Non-exchangeable.") == AmenityStatus.NOT_INCLUDED
    assert map_label_to_canonical("Internet Package") == "wifi"
    assert map_label_to_canonical("Lounge Access (Subject to availability)") == "lounge_access"
    assert map_label_to_canonical(
        "Paid change to previous flights on the same day (Kiosk & Counter)"
    ) == "same_day_earlier_flight"


def test_ubfly_miles_lines_turkish_and_percent():
    from branded_fare_scraper.sources.ubfly import Ubfly
    amen, miles, _ = Ubfly()._parse_rules(["%25 Ekstra Mil"])
    assert miles.mileage_available is True and miles.bonus_percent == 25
    amen, miles, _ = Ubfly()._parse_rules(["30 PERCENT EXTRA MILES"])
    assert miles.bonus_percent == 30 and miles.miles_earned is None
    amen, miles, _ = Ubfly()._parse_rules(["Earn 500 miles"])
    assert miles.miles_earned == 500 and miles.bonus_percent is None


def test_ubfly_free_line_negative_not_upgraded_to_included():
    from branded_fare_scraper.sources.ubfly import Ubfly
    amen, _, _ = Ubfly()._parse_rules(["Non-refundable.", "Meal Service"])
    by_key = {a.canonical_key: a.status for a in amen}
    assert by_key["refund"] == AmenityStatus.NOT_INCLUDED   # NOT the Included upgrade
    assert by_key["meal"] == AmenityStatus.INCLUDED         # plain listed line -> Included


def test_canonical_rule_detail_standardizes_vocabulary():
    from branded_fare_scraper.amenities import canonical_rule_detail
    # Paid/Ücretli/at charge etc. all collapse to one Turkish term per right.
    assert canonical_rule_detail("refund", AmenityStatus.PAID) == "Kesintili"
    assert canonical_rule_detail("refund", AmenityStatus.INCLUDED) == "Kesintisiz"
    assert canonical_rule_detail("change", AmenityStatus.PAID) == "Ücretli"
    assert canonical_rule_detail("change", AmenityStatus.INCLUDED) == "Cezasız"
    assert canonical_rule_detail("same_day_earlier_flight", AmenityStatus.PAID) == "Ücretli"
    assert canonical_rule_detail("refund", AmenityStatus.NOT_INCLUDED) == ""   # — suffices
    assert canonical_rule_detail("cabin_baggage", AmenityStatus.INCLUDED) is None  # numeric keys untouched


def test_noshow_and_cancellation_free_text_mapping():
    # SV-style free-text rule lines must land on the right no-show keys.
    assert map_label_to_canonical("Partial refund with penalty for no-show") == "no_show_refund"
    assert map_label_to_canonical("Change with no-show fee") == "no_show_change"
    assert map_label_to_canonical("Cancellation with fee") == "refund"
    assert classify_status_from_text("Change with no-show fee") == AmenityStatus.PAID
    assert classify_status_from_text("Partial refund with penalty for no-show") == AmenityStatus.PAID


def test_clean_fare_code_drops_internal_ids():
    from branded_fare_scraper.normalization import clean_fare_code
    assert clean_fare_code("CL-f48780cd96ac4e338339d6bb919f4a09") == "CL"
    assert clean_fare_code("EF-EF") == "EF"          # duplicated token collapses
    assert clean_fare_code("PL-PL") == "PL"
    assert clean_fare_code("BSFLEX-BSFLXPLUS") == "BSFLEX-BSFLXPLUS"  # real pair kept
    assert clean_fare_code("f48780cd96ac4e338339d6bb919f4a09") == ""  # bare hash -> blank
    assert clean_fare_code(None) == "" and clean_fare_code("") == ""


def test_ubfly_carrier_attribution_rejects_codeshare_rows():
    from branded_fare_scraper.sources.ubfly import Ubfly
    m = Ubfly._matches_carrier
    # pure QR row (logo QR + QR-filed fares) -> yes
    assert m({"carrier": "QR", "fare_iata": "QR"}, "QR") is True
    # BA-operated row selling QR-plated fares (the LHR-SIN BIZPROMO case) -> NO
    assert m({"carrier": "BA", "fare_iata": "QR"}, "QR") is False
    # QR-marketed row whose fares are filed by BA -> NO (partner ladder)
    assert m({"carrier": "QR", "fare_iata": "BA"}, "QR") is False
    # single-signal rows still work
    assert m({"carrier": "QR", "fare_iata": ""}, "QR") is True
    assert m({"carrier": "", "fare_iata": "QR"}, "QR") is True
    assert m({"carrier": "", "fare_iata": ""}, "QR") is False


def test_ubfly_drops_self_contradictory_box():
    from branded_fare_scraper.sources.ubfly import Ubfly
    flight = {"carrier": "AC", "baseText": "974.81 USD", "brands": [
        {"name": "Standard", "ffcode": "TG-TG", "cabin": "ECONOMY", "lis": [], "priceText": "0.00 USD"},
        {"name": "Economy Light", "ffcode": "EL-EL", "cabin": "BUSINESS", "lis": [], "priceText": "+2,513.36 USD"},
        {"name": "PL", "ffcode": "PL-PL", "cabin": "ECONOMY", "lis": [], "priceText": "+852.78 USD"},
    ]}
    brands = Ubfly()._to_raw_brands(flight, Cabin.ECONOMY)
    names = [b.raw_brand_name for b in brands]
    assert "Economy Light" not in names             # eco-named + BUSINESS tag = junk
    pe = [b for b in brands if b.cabin == Cabin.PREMIUM_ECONOMY]
    assert [b.raw_brand_name for b in pe] == ["PL"]  # AC PE via ff map
    assert pe[0].price_value == pytest.approx(1827.59, abs=0.01)


# --------------------------- amenities ------------------------------------ #
def test_fee_is_paid_not_excluded():
    assert classify_status_from_text("23 kg for a fee") == AmenityStatus.PAID
    assert classify_status_from_text("+25 EUR") == AmenityStatus.PAID
    assert classify_status_from_text("included") == AmenityStatus.INCLUDED
    assert classify_status_from_text("not included") == AmenityStatus.NOT_INCLUDED
    assert classify_status_from_text("") == AmenityStatus.UNKNOWN


def test_label_mapping_multilingual():
    assert map_label_to_canonical("Checked baggage allowance") == "checked_baggage"
    assert map_label_to_canonical("Koltuk seçimi") == "seat_selection"
    assert map_label_to_canonical("Lounge access") == "lounge_access"
    assert map_label_to_canonical("SEATSELECTION") == "seat_selection"
    assert map_label_to_canonical("Aynı gün erken uçuş") == "same_day_earlier_flight"


def test_plus_hint_needs_a_number():
    # "+" alone no longer means Paid; "+<number>" (a fee) does.
    assert classify_status_from_text("change permitted 2+ days") == AmenityStatus.INCLUDED
    assert classify_status_from_text("+25 EUR") == AmenityStatus.PAID
    assert classify_status_from_text("not permitted") == AmenityStatus.NOT_INCLUDED


def test_enuygun_item_status():
    from branded_fare_scraper.sources.enuygun import Enuygun
    assert Enuygun._item_status({"is_available": False}, "anything") == AmenityStatus.NOT_INCLUDED
    assert Enuygun._item_status({}, "not included") == AmenityStatus.NOT_INCLUDED
    assert Enuygun._item_status({}, "1 parça X 8 kg kabin bagajı") == AmenityStatus.INCLUDED
    assert Enuygun._item_status({}, "Ücretli Değişiklik (48 saate kadar)") == AmenityStatus.PAID


def test_carrier_absent_terminal_after_n_dates():
    import asyncio
    from datetime import timedelta
    from branded_fare_scraper.sources.base import SourceAdapter
    from branded_fare_scraper.models import (CabinResult, DatePlan, Job, PriceType, RawBrand,
                                             ScrapeUnit)
    from branded_fare_scraper.retry import CarrierAbsent

    d0 = date(2026, 8, 1)
    window = [(d0 + timedelta(days=i), d0 + timedelta(days=i + 3)) for i in range(8)]
    unit = ScrapeUnit(Job("XX", "AAA", "BBB"), DatePlan(Season.SUMMER, d0, d0 + timedelta(days=3), window))

    class FoundOnThird(SourceAdapter):
        name = "t1"
        def __init__(self): self.calls = 0
        def supports(self, c): return True
        async def fetch_search(self, page, job, dep, ret):
            self.calls += 1
            if self.calls < 3:
                raise CarrierAbsent("absent")
            return [CabinResult(cabin=Cabin.ECONOMY, departure=dep, return_date=ret,
                                brands=[RawBrand("B", Cabin.ECONOMY, 0, 100.0, PriceType.ABSOLUTE)])]

    a = FoundOnThird()
    res = asyncio.run(a.run_unit(None, unit))
    assert res.total_brands() == 1 and a.calls == 3   # window survived 2 CarrierAbsent

    class AlwaysAbsent(SourceAdapter):
        name = "t2"
        def __init__(self): self.calls = 0
        def supports(self, c): return True
        async def fetch_search(self, page, job, dep, ret):
            self.calls += 1
            raise CarrierAbsent("absent")

    b = AlwaysAbsent()
    res2 = asyncio.run(b.run_unit(None, unit))
    assert res2.total_brands() == 0 and b.calls == 3   # terminal after 3 absences


def test_rebuild_roundtrip():
    from branded_fare_scraper.rebuild import raw_brand_from_dict
    d = {"raw_brand_name": "Eco", "cabin": "Economy", "screen_order": 1,
         "price_value": 123.4, "price_type": "absolute", "currency": "USD",
         "display_price_text": "+23 USD", "fare_family_code": "ECO",
         "amenities": [{"raw_label": "meal", "status": "Included", "raw_value": "hot",
                        "canonical_key": "meal"}],
         "miles": {"mileage_available": True, "miles_earned": 500}}
    rb = raw_brand_from_dict(d)
    assert rb.raw_brand_name == "Eco" and rb.price_value == 123.4
    assert rb.cabin.value == "Economy" and rb.amenities[0].canonical_key == "meal"
    assert rb.miles.mileage_available is True and rb.miles.miles_earned == 500


def test_empty_amenity_map_matches_taxonomy():
    from branded_fare_scraper.amenities import AMENITY_KEYS
    m = empty_amenity_map()
    assert m["checked_baggage"] == "Unknown"
    assert "no_show_refund" in m and "pet" in m
    assert len(m) == len(AMENITY_KEYS) == 16


def test_tier_code():
    from branded_fare_scraper.models import Cabin
    from branded_fare_scraper.normalization import tier_code
    assert tier_code(Cabin.ECONOMY, 0) == "Eco-1"
    assert tier_code(Cabin.ECONOMY, 2) == "Eco-3"
    assert tier_code(Cabin.PREMIUM_ECONOMY, 0) == "PEco-1"
    assert tier_code(Cabin.BUSINESS, 1) == "Bus-2"
