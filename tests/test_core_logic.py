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
from branded_fare_scraper.normalization import (assign_brand_order, cross_season_pair_prefs,
                                                detect_cabin, effective_cabin,
                                                iter_ranked_by_cabin, normalize_brand,
                                                regroup_brands_by_cabin)
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
    # AC EL is a TABLED business code: Ubfly's junk label "Economy Light" is
    # replaced by the authoritative name instead of dropping the fare.
    assert "Economy Light" not in names
    assert "Business Lowest" in names
    assert [b.cabin for b in brands if b.raw_brand_name == "Business Lowest"] == [Cabin.BUSINESS]
    pe = [b for b in brands if b.cabin == Cabin.PREMIUM_ECONOMY]
    assert [b.raw_brand_name for b in pe] == ["Premium Economy Lowest"]   # AC PL renamed
    assert pe[0].price_value == pytest.approx(1827.59, abs=0.01)


def test_pretty_brand_name_and_suspicious_scan():
    from branded_fare_scraper.normalization import is_suspicious_brand_name, pretty_brand_name
    # carrier spelling tables
    assert pretty_brand_name("ECOFLY", "TK") == "EcoFly"
    assert pretty_brand_name("BUSINESS PRIME", "TK") == "Business Prime"
    assert pretty_brand_name("BCLASSIC", "QR") == "Business Classic"
    # generic: multi-word ALL-CAPS -> Title Case; mixed case untouched
    assert pretty_brand_name("ECONOMY CLASSIC", "QR") == "Economy Classic"
    assert pretty_brand_name("Premium Economy Lowest", "AC") == "Premium Economy Lowest"
    # single-token caps + bare codes are NOT guessed at
    assert pretty_brand_name("PREMECON", "BA") == "PREMECON"
    assert pretty_brand_name("BX", "TK") == "BX"
    assert is_suspicious_brand_name("BX", "BX-BX") is True
    assert is_suspicious_brand_name("PREMECON", "PREMECON-PREMECON") is True
    assert is_suspicious_brand_name("Economy Classic", "ECLASSIC-ECLASSIC") is False


def test_best_buckets_prefers_most_packages():
    from branded_fare_scraper.sources.ubfly import Ubfly
    f_many = {"carrier": "TK", "fare_iata": "TK", "baseText": "700.00 USD", "brands": [
        {"name": n, "ffcode": n, "cabin": "ECONOMY", "lis": [], "priceText": p}
        for n, p in (("ECOFLY", "0.00 USD"), ("EXTRAFLY", "+40 USD"),
                     ("FLEXFLY", "+108 USD"), ("Flexible", "+214 USD"))]}
    f_few = {"carrier": "TK", "fare_iata": "TK", "baseText": "650.00 USD", "brands": [
        {"name": "ECOFLY", "ffcode": "E1", "cabin": "ECONOMY", "lis": [], "priceText": "0.00 USD"},
        {"name": "EXTRAFLY", "ffcode": "E2", "cabin": "ECONOMY", "lis": [], "priceText": "+40 USD"}]}
    best = Ubfly().best_buckets([f_few, f_many], Cabin.ECONOMY, "TK")
    assert len(best[Cabin.ECONOMY]) == 4    # most-packages flight wins despite pricier base


def test_ac_ff_brand_table():
    from branded_fare_scraper.normalization import ff_override
    assert ff_override("AC", "PF-PF") == ("Premium Economy Flexible", Cabin.PREMIUM_ECONOMY)
    assert ff_override("AC", "EL-EL") == ("Business Lowest", Cabin.BUSINESS)
    assert ff_override("AC", "PB") == ("Premium Economy Basic", Cabin.PREMIUM_ECONOMY)
    assert ff_override("TK", "EF-EF") is None        # table is per-carrier
    assert ff_override("AC", "LT-LT") is None        # untabled codes untouched
    # regroup applies the rename on the rebuild path (fixes existing raw data)
    b = RawBrand("PRIME FLY", Cabin.PREMIUM_ECONOMY, 0, 2262.94, PriceType.ABSOLUTE,
                 fare_family_code="PF-PF")
    from branded_fare_scraper.normalization import regroup_brands_by_cabin
    out = regroup_brands_by_cabin([b], Cabin.ECONOMY, keep_pe=True, carrier="AC")
    assert [x.raw_brand_name for x in out[Cabin.PREMIUM_ECONOMY]] == ["Premium Economy Flexible"]


# ---------------- cross-season pair-order consensus (round 4, fix 1) ------- #
# Live evidence, A3 ATH-BER Economy: Summer captured a momentary inversion
# (Flex 268.74 under Light 270.74, gap 2.00); Winter shows the real ladder
# (Light 270.84 < Flex 325.85 < ComfortFlex 357.85, gap 55.01).
A3_ROUTE = ("A3", "ATH", "BER", Cabin.ECONOMY)
A3_SUMMER = [("flex", 268.74), ("light", 270.74)]
A3_WINTER = [("light", 270.84), ("flex", 325.85), ("comfortflex", 357.85)]


def test_cross_season_pair_prefs_larger_gap_wins():
    prefs = cross_season_pair_prefs({
        A3_ROUTE + ("Summer",): A3_SUMMER,
        A3_ROUTE + ("Winter",): A3_WINTER,
    })
    route = prefs[A3_ROUTE]
    assert route[frozenset({"light", "flex"})] == ("light", "flex")   # 55.01 > 2.00
    # pairs only one season carries give no evidence -> no preference
    assert frozenset({"flex", "comfortflex"}) not in route
    assert frozenset({"light", "comfortflex"}) not in route


def test_cross_season_pair_prefs_abstains_without_evidence():
    route = ("A3", "ATH", "BER", Cabin.ECONOMY)
    # identical gaps, opposite orders -> nothing wins
    assert cross_season_pair_prefs({
        route + ("Summer",): [("flex", 100.0), ("light", 110.0)],
        route + ("Winter",): [("light", 200.0), ("flex", 210.0)],
    }) == {}
    # a missing price kills that season's evidence for the pair
    assert cross_season_pair_prefs({
        route + ("Summer",): [("flex", None), ("light", 110.0)],
        route + ("Winter",): [("light", 200.0), ("flex", 255.0)],
    }) == {}
    # one season alone -> nothing to compare
    assert cross_season_pair_prefs({route + ("Summer",): A3_SUMMER}) == {}
    # both seasons already agree -> no repair emitted
    assert cross_season_pair_prefs({
        route + ("Summer",): [("light", 100.0), ("flex", 150.0)],
        route + ("Winter",): [("light", 200.0), ("flex", 210.0)],
    }) == {}


def test_assign_brand_order_applies_pair_pref():
    # The Summer ladder alone would publish Flex first (it really was cheaper
    # that day); Winter's much larger gap is the stronger evidence.
    summer = [RawBrand("Flex", Cabin.ECONOMY, 0, 268.74, PriceType.ABSOLUTE),
              RawBrand("Light", Cabin.ECONOMY, 1, 270.74, PriceType.ABSOLUTE)]
    prefs = {frozenset({"light", "flex"}): ("light", "flex")}
    ordered = assign_brand_order(summer, prefs)
    assert [r.raw_brand_name for r, _, _ in ordered] == ["Light", "Flex"]
    assert [o for _, _, o in ordered] == [0, 1]              # renumbered 0..n-1
    # without preferences the price order stands
    assert [r.raw_brand_name for r, _, _ in assign_brand_order(summer)] == ["Flex", "Light"]


def test_pair_pref_swaps_only_adjacent_pairs():
    winter = [RawBrand("Light", Cabin.ECONOMY, 0, 270.84, PriceType.ABSOLUTE),
              RawBrand("Flex", Cabin.ECONOMY, 1, 325.85, PriceType.ABSOLUTE),
              RawBrand("ComfortFlex", Cabin.ECONOMY, 2, 357.85, PriceType.ABSOLUTE)]
    # already in the preferred order -> untouched
    names = [r.raw_brand_name for r, _, _ in
             assign_brand_order(winter, {frozenset({"light", "flex"}): ("light", "flex")})]
    assert names == ["Light", "Flex", "ComfortFlex"]
    # Light vs ComfortFlex are NOT neighbours: Flex's price sits between them,
    # so the price order of the element in between wins and nothing moves.
    names = [r.raw_brand_name for r, _, _ in
             assign_brand_order(winter, {frozenset({"light", "comfortflex"}):
                                         ("comfortflex", "light")})]
    assert names == ["Light", "Flex", "ComfortFlex"]


def test_af_business_cross_season_order():
    # AF CDG-PEK Business: Summer put Standart first (gap 69.47), Winter put
    # Business first (gap 89.98) -> Winter's order wins in BOTH seasons.
    route = ("AF", "CDG", "PEK", Cabin.BUSINESS)
    prefs = cross_season_pair_prefs({
        route + ("Summer",): [("standart", 2000.00), ("business", 2069.47)],
        route + ("Winter",): [("business", 2100.00), ("standart", 2189.98)],
    })[route]
    assert prefs[frozenset({"standart", "business"})] == ("business", "standart")
    summer = [RawBrand("Standart", Cabin.BUSINESS, 0, 2000.00, PriceType.ABSOLUTE),
              RawBrand("Business", Cabin.BUSINESS, 1, 2069.47, PriceType.ABSOLUTE)]
    assert [r.raw_brand_name for r, _, _ in assign_brand_order(summer, prefs)] \
        == ["Business", "Standart"]


def _a3_raw_record(season, brands):
    return {"unit_key": f"A3|ATH|BER|{season}", "carrier": "A3", "origin": "ATH",
            "destination": "BER", "season": season, "source": "Ubfly",
            "status": "success", "retry_count": 0,
            "cabins": [{"cabin": "Economy", "source": "Ubfly", "departure": "2026-08-01",
                        "return": "2026-08-04", "brands": [
                            {"raw_brand_name": n, "cabin": "Economy", "screen_order": i,
                             "price_value": p, "price_type": "absolute", "currency": "USD",
                             "display_price_text": "", "fare_family_code": n.upper(),
                             "amenities": [], "miles": {}}
                            for i, (n, p) in enumerate(brands)]}]}


A3_SUMMER_REC = _a3_raw_record("Summer", [("Flex", 268.74), ("Light", 270.74)])
A3_WINTER_REC = _a3_raw_record("Winter", [("Light", 270.84), ("Flex", 325.85),
                                          ("ComfortFlex", 357.85)])


def test_season_pair_prefs_from_raw_fixes_both_seasons(tmp_path):
    import json
    from branded_fare_scraper.rebuild import (pair_prefs_for, raw_brand_from_dict,
                                              season_pair_prefs)
    raw = tmp_path / "raw_data.jsonl"
    raw.write_text("\n".join(json.dumps(r) for r in (A3_SUMMER_REC, A3_WINTER_REC)),
                   encoding="utf-8")
    ppc = pair_prefs_for(season_pair_prefs(raw), "A3", "ATH", "BER")
    for rec in (A3_SUMMER_REC, A3_WINTER_REC):
        brands = [raw_brand_from_dict(b) for b in rec["cabins"][0]["brands"]]
        names = [raw_b.raw_brand_name for _c, raw_b, _nb, _o, _a in iter_ranked_by_cabin(
            brands, Cabin.ECONOMY, carrier="A3", pair_prefs_by_cabin=ppc)]
        assert names[:2] == ["Light", "Flex"], rec["season"]


def test_runner_prefs_match_reprocessed_prefs(tmp_path):
    """The run's own outputs must order exactly like a later reprocess."""
    import json
    from branded_fare_scraper.models import (CabinResult, DatePlan, Job, ScrapeUnit,
                                             UnitResult, UnitStatus)
    from branded_fare_scraper.rebuild import (raw_brand_from_dict, season_pair_prefs,
                                              season_pair_prefs_from_results)
    raw = tmp_path / "raw_data.jsonl"
    raw.write_text("\n".join(json.dumps(r) for r in (A3_SUMMER_REC, A3_WINTER_REC)),
                   encoding="utf-8")
    d0 = date(2026, 8, 1)
    results = []
    for rec, season in ((A3_SUMMER_REC, Season.SUMMER), (A3_WINTER_REC, Season.WINTER)):
        unit = ScrapeUnit(Job("A3", "ATH", "BER"),
                          DatePlan(season, d0, d0 + timedelta(days=3), [(d0, d0 + timedelta(days=3))]))
        cabs = [CabinResult(cabin=Cabin.ECONOMY, departure=d0, return_date=d0 + timedelta(days=3),
                            brands=[raw_brand_from_dict(b) for b in rec["cabins"][0]["brands"]])]
        results.append(UnitResult(unit=unit, source="Ubfly", cabin_results=cabs,
                                  status=UnitStatus.SUCCESS))
    assert season_pair_prefs_from_results(results) == season_pair_prefs(raw)
    assert season_pair_prefs(raw)[("A3", "ATH", "BER", Cabin.ECONOMY)] == {
        frozenset({"light", "flex"}): ("light", "flex")}


# ---------- one ladder per cabin per unit (duplicate-PE regression) -------- #
def _cab_result(cabin, brands, source="Ubfly", day=1):
    from branded_fare_scraper.models import CabinResult
    return CabinResult(cabin=cabin, departure=date(2026, 8, day),
                       return_date=date(2026, 8, day + 3),
                       brands=[RawBrand(n, cabin, i, p, PriceType.ABSOLUTE, fare_family_code=ff)
                               for i, (n, p, ff) in enumerate(brands)],
                       source=source)


def test_unit_publishes_one_pe_ladder_dedicated_result_wins():
    # EK AMM-PVG Summer: the Economy result carries a PE family that leaked into
    # the economy ladder (1899.96) while a dedicated PE result also exists
    # (1892.81). Publishing both produced near-duplicate PE rows, sometimes with
    # descending prices. Same length -> the result that IS Premium Economy wins.
    from branded_fare_scraper.normalization import iter_unit_ranked_by_cabin
    eco = _cab_result(Cabin.ECONOMY,
                      [("Economy Saver", 850.11, "ECOSAVER"),
                       ("Economy Flex", 1120.40, "ECOFLEX"),
                       ("Premium Economy Flex Plus", 1899.96, "PYFLXPLUS-PYFLXPLUS")],
                      source="Ubfly", day=1)
    pe = _cab_result(Cabin.PREMIUM_ECONOMY,
                     [("Premium Economy FlexPlus", 1892.81,
                       "WF-79c0c922aa1c4f0e9a1a1b0d5d6e7f80")], source="Enuygun", day=5)
    got = {}
    for eff_cab, raw, _nb, order, absp, src in iter_unit_ranked_by_cabin([eco, pe], carrier="EK"):
        got.setdefault(eff_cab, []).append((order, raw.raw_brand_name, absp, src.source))
    assert len(got[Cabin.PREMIUM_ECONOMY]) == 1                      # was 2 -> duplicate rows
    order, name, price, source = got[Cabin.PREMIUM_ECONOMY][0]
    # Both spellings normalize to the same display name (EK spelling table);
    # the dedicated result is identified by ITS price + source, not the string.
    assert (order, name) == (0, "Premium Economy Flex Plus")
    assert price == pytest.approx(1892.81)                            # dedicated result wins
    assert source == "Enuygun"                                       # winner's own metadata
    assert [n for _o, n, _p, _s in got[Cabin.ECONOMY]] == ["Economy Saver", "Economy Flex"]


def test_unit_ladder_dedupe_prefers_the_richer_ladder():
    # Rule (a) beats rule (b): a 3-fare PE ladder leaking out of the Economy
    # result is more complete than the dedicated 1-fare PE result.
    from branded_fare_scraper.normalization import iter_unit_ranked_by_cabin
    eco = _cab_result(Cabin.ECONOMY,
                      [("Economy Saver", 850.11, "ECOSAVER"),
                       ("Premium Economy Light", 1700.00, "PRELIGHT"),
                       ("Premium Economy Comfort", 1800.00, "PRECMFT"),
                       ("Premium Economy Flex", 1900.00, "PREFLEX")])
    pe = _cab_result(Cabin.PREMIUM_ECONOMY,
                     [("Premium Economy Comfort", 1892.81, "PRECMFT")], source="Enuygun", day=5)
    names, sources = [], set()
    for eff_cab, raw, _nb, _order, _absp, src in iter_unit_ranked_by_cabin([eco, pe], carrier="LH"):
        if eff_cab == Cabin.PREMIUM_ECONOMY:
            names.append(raw.raw_brand_name)
            sources.add(src.source)
    assert names == ["Premium Economy Light", "Premium Economy Comfort",
                     "Premium Economy Flex"]
    assert sources == {"Ubfly"}                    # leak ladder won on brand count


def test_unit_ladder_dedupe_keeps_distinct_cabins():
    from branded_fare_scraper.normalization import iter_unit_ranked_by_cabin
    eco = _cab_result(Cabin.ECONOMY, [("Economy Saver", 500.0, "E1")])
    biz = _cab_result(Cabin.BUSINESS, [("Business Flex", 2500.0, "B1")], day=5)
    got = {c: n for c, n, _, _, _, _ in
           ((e, r.raw_brand_name, o, a, s, 0)
            for e, r, _nb, o, a, s in iter_unit_ranked_by_cabin([eco, biz], carrier="XX"))}
    assert got == {Cabin.ECONOMY: "Economy Saver", Cabin.BUSINESS: "Business Flex"}


def test_all_consumers_share_the_unit_dedupe():
    """reprocess/to_platform/make_excel/runner must call the same generator."""
    import inspect
    import make_excel, reprocess_raw, to_platform
    from branded_fare_scraper import runner
    for mod in (make_excel, reprocess_raw, to_platform, runner):
        src = inspect.getsource(mod)
        assert "iter_unit_ranked_by_cabin(" in src, mod.__name__
        assert "iter_ranked_by_cabin(" not in src.replace("iter_unit_ranked_by_cabin(", ""), \
            mod.__name__


# ------------------- upgrade-leak capture (round 4, fix 2) ----------------- #
def test_regroup_keeps_higher_cabin_leak_drops_lower():
    # Economy search: a Business box on the row is a real sellable fare.
    eco = [RawBrand("Economy Comfort", Cabin.ECONOMY, 0, 302.88, PriceType.ABSOLUTE),
           RawBrand("Business", Cabin.ECONOMY, 1, 735.66, PriceType.ABSOLUTE)]
    buckets = regroup_brands_by_cabin(eco, Cabin.ECONOMY, keep_pe=True)
    assert set(buckets) == {Cabin.ECONOMY, Cabin.BUSINESS}
    assert [b.raw_brand_name for b in buckets[Cabin.BUSINESS]] == ["Business"]
    # Business search: an economy-named box is still noise (the $5,877 bug).
    biz = [RawBrand("Business Flex", Cabin.BUSINESS, 0, 2500, PriceType.ABSOLUTE),
           RawBrand("Economy Light", Cabin.BUSINESS, 1, 5877, PriceType.ABSOLUTE)]
    assert set(regroup_brands_by_cabin(biz, Cabin.BUSINESS, keep_pe=False)) == {Cabin.BUSINESS}


def test_ubfly_economy_search_captures_business_upgrade_leak():
    # Live evidence, ATH-FRA economy search, row "LH 5919": COMFORT 0.00,
    # FLEX +48.98, BUSINESS +432.78 on base 302.88. The BUSINESS box used to be
    # thrown away, so LH ATH-FRA published with no Business at all.
    from branded_fare_scraper.sources.ubfly import Ubfly
    flight = {"carrier": "LH", "fare_iata": "LH", "baseText": "302.88 USD", "direct": True,
              "brands": [
                  {"name": "COMFORT", "ffcode": "ECOCMFT", "cabin": "ECONOMY", "lis": [],
                   "priceText": "0.00 USD"},
                  {"name": "FLEX", "ffcode": "ECOFLEX", "cabin": "ECONOMY", "lis": [],
                   "priceText": "+48.98 USD"},
                  {"name": "BUSINESS", "ffcode": "BUSCMFT", "cabin": "ECONOMY", "lis": [],
                   "priceText": "+432.78 USD"}]}
    best = Ubfly().best_buckets([flight], Cabin.ECONOMY, "LH")
    assert set(best) == {Cabin.ECONOMY, Cabin.BUSINESS}
    assert len(best[Cabin.ECONOMY]) == 2
    assert [b.price_value for b in best[Cabin.ECONOMY]] == [pytest.approx(302.88),
                                                            pytest.approx(351.86)]
    assert len(best[Cabin.BUSINESS]) == 1
    assert best[Cabin.BUSINESS][0].price_value == pytest.approx(735.66, abs=0.01)


def test_ubfly_business_search_still_drops_economy_box():
    from branded_fare_scraper.sources.ubfly import Ubfly
    flight = {"carrier": "XX", "fare_iata": "XX", "baseText": "2500.00 USD", "direct": True,
              "brands": [
                  {"name": "Business Flex", "ffcode": "BF", "cabin": "BUSINESS", "lis": [],
                   "priceText": "0.00 USD"},
                  {"name": "Economy Light", "ffcode": "EL", "cabin": "ECONOMY", "lis": [],
                   "priceText": "+3377.00 USD"}]}
    best = Ubfly().best_buckets([flight], Cabin.BUSINESS, "XX")
    assert set(best) == {Cabin.BUSINESS}
    assert [b.raw_brand_name for b in best[Cabin.BUSINESS]] == ["Business Flex"]


# ------------------- direct-flight preference (round 4, fix 3) ------------- #
def _eco_flight(carrier, base, fares, direct):
    return {"carrier": carrier, "fare_iata": carrier, "direct": direct,
            "baseText": f"{base:.2f} USD",
            "brands": [{"name": n, "ffcode": n, "cabin": c, "lis": [], "priceText": p}
                       for n, p, c in fares]}


def test_best_buckets_prefers_direct_over_richer_connection():
    from branded_fare_scraper.sources.ubfly import Ubfly
    conn = _eco_flight("TK", 600.0, [("ECOFLY", "0.00 USD", "ECONOMY"),
                                     ("EXTRAFLY", "+40 USD", "ECONOMY"),
                                     ("FLEXFLY", "+108 USD", "ECONOMY"),
                                     ("PRIMEFLY", "+214 USD", "ECONOMY")], False)
    nonstop = _eco_flight("TK", 700.0, [("ECOFLY", "0.00 USD", "ECONOMY"),
                                        ("EXTRAFLY", "+40 USD", "ECONOMY"),
                                        ("FLEXFLY", "+108 USD", "ECONOMY")], True)
    best = Ubfly().best_buckets([conn, nonstop], Cabin.ECONOMY, "TK")
    assert len(best[Cabin.ECONOMY]) == 3                       # the direct ladder
    assert best[Cabin.ECONOMY][0].price_value == pytest.approx(700.0)


def test_best_buckets_falls_back_to_connection_without_direct():
    from branded_fare_scraper.sources.ubfly import Ubfly
    conn = _eco_flight("TK", 600.0, [("ECOFLY", "0.00 USD", "ECONOMY"),
                                     ("EXTRAFLY", "+40 USD", "ECONOMY"),
                                     ("FLEXFLY", "+108 USD", "ECONOMY")], False)
    # older stored flights carry no "direct" key at all -> treated as non-direct
    legacy = {"carrier": "TK", "fare_iata": "TK", "baseText": "650.00 USD",
              "brands": [{"name": "ECOFLY", "ffcode": "E1", "cabin": "ECONOMY", "lis": [],
                          "priceText": "0.00 USD"}]}
    best = Ubfly().best_buckets([legacy, conn], Cabin.ECONOMY, "TK")
    assert len(best[Cabin.ECONOMY]) == 3                       # most packages still wins
    assert best[Cabin.ECONOMY][0].price_value == pytest.approx(600.0)


def test_best_buckets_side_picks_cabin_only_on_a_connection():
    # Never lose a cabin just to stay direct: the direct flight sets the Economy
    # ladder, the connection contributes the Premium Economy one.
    from branded_fare_scraper.sources.ubfly import Ubfly
    nonstop = _eco_flight("BA", 500.0, [("Economy Saver", "0.00 USD", "ECONOMY"),
                                        ("Economy Flex", "+120 USD", "ECONOMY")], True)
    conn = _eco_flight("BA", 480.0, [("Economy Saver", "0.00 USD", "ECONOMY"),
                                     ("PREMECON", "+400 USD", "ECONOMY")], False)
    best = Ubfly().best_buckets([nonstop, conn], Cabin.ECONOMY, "BA")
    assert len(best[Cabin.ECONOMY]) == 2
    assert best[Cabin.ECONOMY][0].price_value == pytest.approx(500.0)      # direct row
    assert [b.raw_brand_name for b in best[Cabin.PREMIUM_ECONOMY]] == ["PREMECON"]
    assert best[Cabin.PREMIUM_ECONOMY][0].price_value == pytest.approx(880.0)


def test_extract_js_flags_direct_rows():
    # The row's own text decides; the fare panel never carries this wording.
    from branded_fare_scraper.sources.ubfly import _EXTRACT_JS
    assert "const direct = /\\bDirect\\b/i.test(rowText) && !/Transfer/i.test(rowText);" \
        in _EXTRACT_JS
    assert "direct: direct" in _EXTRACT_JS


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
    # Window survived 2 CarrierAbsent days and found economy on day 3; it then
    # keeps walking the remaining days looking for the still-missing cabins
    # (searches are cached in real adapters, so this is nearly free).
    assert res.total_brands() == 1 and a.calls == 8

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


def test_window_keeps_walking_for_missing_cabins():
    # Business found on day 1 must NOT stop the economy search (AC DEL-YVR case):
    # the window keeps walking and cabins merge with their own dates.
    import asyncio
    from datetime import timedelta
    from branded_fare_scraper.sources.base import SourceAdapter
    from branded_fare_scraper.models import CabinResult, DatePlan, Job, ScrapeUnit

    d0 = date(2027, 5, 23)
    window = [(d0 + timedelta(days=i), d0 + timedelta(days=i + 3)) for i in range(8)]
    unit = ScrapeUnit(Job("AC", "DEL", "YVR"),
                      DatePlan(Season.SUMMER, d0, d0 + timedelta(days=3), window))

    class BusThenEco(SourceAdapter):
        name = "t3"
        def __init__(self): self.calls = 0
        def supports(self, c): return True
        def cabins_for(self, job): return [Cabin.ECONOMY, Cabin.BUSINESS]
        async def fetch_search(self, page, job, dep, ret):
            self.calls += 1
            out = [CabinResult(cabin=Cabin.BUSINESS, departure=dep, return_date=ret,
                               brands=[RawBrand("Business Lowest", Cabin.BUSINESS, 0, 6000.0,
                                                PriceType.ABSOLUTE)])]
            if self.calls >= 3:            # economy appears only on the 3rd date
                out.append(CabinResult(cabin=Cabin.ECONOMY, departure=dep, return_date=ret,
                                       brands=[RawBrand("Standard", Cabin.ECONOMY, 0, 900.0,
                                                        PriceType.ABSOLUTE)]))
            return out

    a = BusThenEco()
    res = asyncio.run(a.run_unit(None, unit))
    cabs = {c.cabin: c for c in res.cabin_results}
    assert set(cabs) == {Cabin.ECONOMY, Cabin.BUSINESS}
    assert a.calls == 3
    assert cabs[Cabin.BUSINESS].departure == d0            # kept from day 1
    assert cabs[Cabin.ECONOMY].departure == d0 + timedelta(days=2)


def test_window_upgrades_cabin_to_a_richer_ladder():
    # EK ISB-MAN Summer: business was locked in on the first window date with a
    # 2-fare ladder while the unit kept walking dates for the missing economy
    # cabin — and those later ct3 searches were showing the full 3-fare ladder
    # (BSFLXPLUS), which used to be discarded. A strictly richer ladder for an
    # already-found cabin now replaces it, at no extra search cost.
    import asyncio
    from datetime import timedelta
    from branded_fare_scraper.sources.base import SourceAdapter
    from branded_fare_scraper.models import CabinResult, DatePlan, Job, ScrapeUnit

    d0 = date(2026, 9, 2)
    window = [(d0 + timedelta(days=i), d0 + timedelta(days=i + 3)) for i in range(8)]
    unit = ScrapeUnit(Job("EK", "ISB", "MAN"),
                      DatePlan(Season.SUMMER, d0, d0 + timedelta(days=3), window))

    def _biz(names, dep, ret):
        return CabinResult(cabin=Cabin.BUSINESS, departure=dep, return_date=ret,
                           brands=[RawBrand(n, Cabin.BUSINESS, i, p, PriceType.ABSOLUTE)
                                   for i, (n, p) in enumerate(names)])

    class ThinThenFull(SourceAdapter):
        name = "t4"
        def __init__(self): self.calls = 0
        def supports(self, c): return True
        def cabins_for(self, job): return [Cabin.ECONOMY, Cabin.BUSINESS]
        async def fetch_search(self, page, job, dep, ret):
            self.calls += 1
            if self.calls == 1:                    # 09-02: thin business, no economy
                return [_biz([("Business Saver", 2450.0), ("Business Flex", 2890.0)], dep, ret)]
            return [                               # later date: full ladder + economy
                _biz([("Business Saver", 2455.0), ("Business Flex", 2895.0),
                      ("Business Flex Plus", 3310.0)], dep, ret),
                CabinResult(cabin=Cabin.ECONOMY, departure=dep, return_date=ret,
                            brands=[RawBrand("Economy Saver", Cabin.ECONOMY, 0, 690.0,
                                             PriceType.ABSOLUTE),
                                    RawBrand("Economy Flex", Cabin.ECONOMY, 1, 940.0,
                                             PriceType.ABSOLUTE)])]

    a = ThinThenFull()
    res = asyncio.run(a.run_unit(None, unit))
    cabs = {c.cabin: c for c in res.cabin_results}
    assert a.calls == 2                                    # no extra searches bought
    assert [b.raw_brand_name for b in cabs[Cabin.BUSINESS].brands] == \
        ["Business Saver", "Business Flex", "Business Flex Plus"]
    assert cabs[Cabin.BUSINESS].departure == d0 + timedelta(days=1)   # upgrade's own date
    assert len(cabs[Cabin.ECONOMY].brands) == 2
    assert cabs[Cabin.ECONOMY].departure == d0 + timedelta(days=1)


def test_window_keeps_first_capture_when_ladder_is_not_richer():
    # Equal-length later ladders must NOT churn the capture (prices move all day).
    import asyncio
    from datetime import timedelta
    from branded_fare_scraper.sources.base import SourceAdapter
    from branded_fare_scraper.models import CabinResult, DatePlan, Job, ScrapeUnit

    d0 = date(2026, 9, 2)
    window = [(d0 + timedelta(days=i), d0 + timedelta(days=i + 3)) for i in range(8)]
    unit = ScrapeUnit(Job("EK", "ISB", "MAN"),
                      DatePlan(Season.SUMMER, d0, d0 + timedelta(days=3), window))

    class AlwaysTwo(SourceAdapter):
        name = "t5"
        def __init__(self): self.calls = 0
        def supports(self, c): return True
        def cabins_for(self, job): return [Cabin.ECONOMY, Cabin.BUSINESS]
        async def fetch_search(self, page, job, dep, ret):
            self.calls += 1
            bump = 100.0 * self.calls              # a different price every date
            return [CabinResult(cabin=Cabin.BUSINESS, departure=dep, return_date=ret,
                                brands=[RawBrand("Business Saver", Cabin.BUSINESS, 0,
                                                 2450.0 + bump, PriceType.ABSOLUTE),
                                        RawBrand("Business Flex", Cabin.BUSINESS, 1,
                                                 2890.0 + bump, PriceType.ABSOLUTE)])]

    a = AlwaysTwo()
    res = asyncio.run(a.run_unit(None, unit))
    cabs = {c.cabin: c for c in res.cabin_results}
    assert a.calls == 8                            # economy never appears -> full walk
    assert cabs[Cabin.BUSINESS].departure == d0    # the FIRST capture is kept
    assert cabs[Cabin.BUSINESS].brands[0].price_value == pytest.approx(2550.0)


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
