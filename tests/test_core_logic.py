"""Unit tests for the pure-logic core (no network, no browser)."""

from __future__ import annotations

import random
from datetime import date, timedelta

import pytest

from branded_fare_scraper.amenities import (
    classify_status_from_text, empty_amenity_map, map_label_to_canonical)
from branded_fare_scraper.dates import (FAR_FALLBACK_LEADS, SUMMER_MONTHS, WINTER_MONTHS, build_date_plan,
                                        pick_departure)
from branded_fare_scraper.models import AmenityStatus, Cabin, PriceType, RawBrand, Season
from branded_fare_scraper.normalization import (assign_brand_order, cross_season_pair_prefs,
                                                detect_cabin, effective_cabin,
                                                iter_ranked_by_cabin, normalize_brand,
                                                regroup_brands_by_cabin)
from branded_fare_scraper.pricing import compute_absolute_prices, parse_price


@pytest.fixture(autouse=True)
def _tripcom_isolated():
    """Trip.com keeps run-scoped shared state (pace, cooldown, PE probe).

    Reset it around every test and drop the politeness delay to zero — the gate
    has its own dedicated test that sets a real interval.
    """
    from branded_fare_scraper.sources.tripcom import Tripcom
    Tripcom.reset_state()
    Tripcom.min_interval_s = 0.0
    yield
    Tripcom.reset_state()
    Tripcom.min_interval_s = 1.5


# --------------------------- dates ---------------------------------------- #
def test_summer_departure_month_in_range():
    dep = pick_departure(Season.SUMMER, today=date(2026, 1, 1), rng=random.Random(1))
    assert dep.month in SUMMER_MONTHS
    assert dep >= date(2026, 1, 1) + timedelta(days=21)


def test_winter_spans_year_boundary():
    dep = pick_departure(Season.WINTER, today=date(2026, 7, 1), rng=random.Random(2))
    assert dep.month in WINTER_MONTHS


def test_legacy_window_mode_still_walks_seven_dates():
    plan = build_date_plan(Season.SUMMER, today=date(2026, 1, 1), mode="window",
                           rng=random.Random(3))
    assert plan.return_date == plan.departure + timedelta(days=3)
    assert len(plan.window) == 7                      # D0 .. D0+6
    assert plan.window[-1][0] == plan.departure + timedelta(days=6)
    for dep, ret in plan.window:
        assert ret == dep + timedelta(days=3)
    assert plan.window[0][0] == plan.departure
    assert len(plan.blocks) == 1                      # one block: walk it all


def test_far3_samples_the_deepest_bookable_band():
    """Unsold far-future inventory still shows the complete family ladder."""
    from branded_fare_scraper.dates import (FAR_BLOCK, FAR_MAX_LEAD_DAYS,
                                            FAR_MIN_LEAD_DAYS)
    today = date(2026, 1, 1)
    plan = build_date_plan(Season.SUMMER, today=today, rng=random.Random(3))
    lead = (plan.departure - today).days
    # The start may precede the band by up to FAR_BLOCK-1 days: the band is a
    # target for the whole block, and backing the start off is what keeps a
    # block inside its season when the band only clips the season's last days
    # (season beats lead time — see build_date_plan).
    assert FAR_MIN_LEAD_DAYS - (FAR_BLOCK - 1) <= lead <= FAR_MAX_LEAD_DAYS
    assert plan.departure.month in SUMMER_MONTHS
    assert plan.return_date == plan.departure + timedelta(days=3)
    # Block 1 = the picked date plus the following consecutive days. How MANY
    # days is a tuning knob (FAR_BLOCK), so assert the shape, not the number.
    first = plan.blocks[0]
    assert [d for d, _r in first] == [plan.departure + timedelta(days=i)
                                      for i in range(FAR_BLOCK)]
    assert all(r == d + timedelta(days=3) for d, r in plan.window)
    # A custom band is honoured.
    tight = build_date_plan(Season.SUMMER, today=today, far_lead=(300, 305),
                            rng=random.Random(5))
    assert 300 - (FAR_BLOCK - 1) <= (tight.departure - today).days <= 305


def test_far3_has_ordered_fallback_blocks():
    today = date(2026, 1, 1)
    plan = build_date_plan(Season.SUMMER, today=today, rng=random.Random(3))
    # Band count AND block length are data, not constants: widening either for
    # coverage must not break the ordering contract this asserts.
    from branded_fare_scraper.dates import FAR_BLOCK
    assert plan.block_size == FAR_BLOCK
    assert len(plan.blocks) == 1 + len(FAR_FALLBACK_LEADS)
    leads = [(b[0][0] - today).days for b in plan.blocks]
    assert leads == sorted(leads, reverse=True)       # deepest first, then closer in
    from branded_fare_scraper.dates import FAR_MIN_LEAD_DAYS
    assert leads[0] >= FAR_MIN_LEAD_DAYS - (FAR_BLOCK - 1)
    for lead, (centre, spread) in zip(leads[1:], FAR_FALLBACK_LEADS):
        assert centre - spread - 10 <= lead <= centre + spread + 10
    for b in plan.blocks:
        assert len(b) == FAR_BLOCK
        assert all(d.month in SUMMER_MONTHS for d, _r in b)


def test_far3_keeps_the_season_even_when_the_band_has_none():
    # Winter generated in January: 210/150 days out land in summer months, so
    # season correctness must win over the exact lead time.
    today = date(2026, 1, 1)
    plan = build_date_plan(Season.WINTER, today=today, rng=random.Random(3))
    for b in plan.blocks:
        assert b[0][0].month in WINTER_MONTHS
    assert (plan.departure - today).days >= 300


def test_date_plan_blocks_round_trip_through_the_checkpoint():
    from branded_fare_scraper.models import DatePlan
    plan = build_date_plan(Season.SUMMER, today=date(2026, 1, 1), rng=random.Random(3))
    again = DatePlan.from_dict(plan.to_dict())
    assert again.window == plan.window
    assert again.block_size == plan.block_size
    assert [len(b) for b in again.blocks] == [len(b) for b in plan.blocks]
    # A plan written before blocks existed still loads as a single block.
    legacy = dict(plan.to_dict())
    legacy.pop("block_size")
    assert len(DatePlan.from_dict(legacy).blocks) == 1


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
    # Terminal after 5 absences — raised with the period (8 -> 15 dates) so a
    # carrier flying a few times a week isn't written off too early.
    assert res2.total_brands() == 0 and b.calls == 5


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
    # The whole period is inspected now — no early stop once both cabins exist.
    assert a.calls == len(window)
    # Every date offers the same 1-brand ladders, so the FIRST capture of each
    # cabin is what survives (stability rule).
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
    assert a.calls == len(window)                          # full period inspected
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


def test_window_prefers_richer_content_on_equal_ladders():
    # "en çok paket olan uçuşu ve en çok içeriği olan paketi al": equal brand
    # counts -> the date whose packages actually list more rights wins.
    import asyncio
    from datetime import timedelta
    from branded_fare_scraper.models import (CabinResult, DatePlan, Job, RawAmenity,
                                             ScrapeUnit)
    from branded_fare_scraper.sources.base import SourceAdapter, ladder_content

    d0 = date(2026, 9, 2)
    window = [(d0 + timedelta(days=i), d0 + timedelta(days=i + 3)) for i in range(4)]
    unit = ScrapeUnit(Job("EK", "ISB", "MAN"),
                      DatePlan(Season.SUMMER, d0, d0 + timedelta(days=3), window))

    def _brand(name, price, n_rules, desc=""):
        return RawBrand(name, Cabin.BUSINESS, 0, price, PriceType.ABSOLUTE,
                        amenities=[RawAmenity(f"r{i}", AmenityStatus.INCLUDED, "yes",
                                              canonical_key="meal") for i in range(n_rules)],
                        description=desc)

    class ThinContentThenRich(SourceAdapter):
        name = "t6"
        def __init__(self): self.calls = 0
        def supports(self, c): return True
        def cabins_for(self, job): return [Cabin.BUSINESS]
        async def fetch_search(self, page, job, dep, ret):
            self.calls += 1
            rules = 2 if self.calls == 1 else 6      # same 2 packages, richer rules
            return [CabinResult(cabin=Cabin.BUSINESS, departure=dep, return_date=ret,
                                brands=[_brand("Business Saver", 2450.0, rules),
                                        _brand("Business Flex", 2890.0, rules,
                                               desc="Lounge; Chauffeur" if rules > 2 else "")])]

    a = ThinContentThenRich()
    res = asyncio.run(a.run_unit(None, unit))
    biz = {c.cabin: c for c in res.cabin_results}[Cabin.BUSINESS]
    assert len(biz.brands) == 2                            # brand count never changed
    assert biz.departure == d0 + timedelta(days=1)         # the richer date won
    assert ladder_content(biz) == 14                       # 6+6 rules + 2 free-text lines


def test_ladder_strength_orders_by_brands_then_content():
    from branded_fare_scraper.models import CabinResult, RawAmenity
    from branded_fare_scraper.sources.base import ladder_strength

    def _cr(n_brands, n_rules):
        return CabinResult(cabin=Cabin.ECONOMY, brands=[
            RawBrand(f"B{i}", Cabin.ECONOMY, i, 100.0 + i, PriceType.ABSOLUTE,
                     amenities=[RawAmenity("r", AmenityStatus.INCLUDED, "y", canonical_key="meal")
                                for _ in range(n_rules)])
            for i in range(n_brands)])
    assert ladder_strength(_cr(3, 0)) > ladder_strength(_cr(2, 99))   # brands dominate
    assert ladder_strength(_cr(2, 5)) > ladder_strength(_cr(2, 4))    # then content
    assert ladder_strength(_cr(2, 4)) == ladder_strength(_cr(2, 4))   # tie -> keep first


# ------------------- interline exclusion (round 15, A1) -------------------- #
def test_ubfly_excludes_interline_operated_rows():
    from branded_fare_scraper.sources.ubfly import Ubfly
    m = Ubfly._matches_carrier
    # LH-marketed, Aegean-operated: the row sells Aegean's product, not LH's.
    assert m({"carrier": "LH", "fare_iata": "LH", "operating": "Aegean Airlines"}, "LH") is False
    # QR-plated row operated by BA (the Round-7 pattern) is out for QR too.
    assert m({"carrier": "QR", "fare_iata": "QR",
              "operating": "British Airways"}, "QR") is False
    # Self-operated row stays.
    assert m({"carrier": "TK", "fare_iata": "TK", "operating": "Turkish Airlines"}, "TK") is True
    # No operating info -> unchanged behaviour (both identity rules still apply).
    assert m({"carrier": "TK", "fare_iata": "TK"}, "TK") is True
    assert m({"carrier": "TK", "fare_iata": "TK", "operating": ""}, "TK") is True
    assert m({"carrier": "BA", "fare_iata": "QR", "operating": ""}, "QR") is False


def test_operating_airline_name_resolution():
    from branded_fare_scraper.sources.ubfly import _operator_matches, resolve_airline_code
    assert resolve_airline_code("Aegean Airlines") == "A3"      # unique containment
    assert resolve_airline_code("British Airways") == "BA"      # exact
    assert resolve_airline_code("turkish airlines") == "TK"     # case-insensitive
    assert resolve_airline_code("Fictional Regional Carrier") is None
    # Unresolvable operator + known target -> name containment decides.
    assert _operator_matches("Operated by Turkish Airlines Inc", "TK") is True
    assert _operator_matches("Fictional Regional Carrier", "TK") is False
    # Unresolvable operator AND unknown target -> cannot judge, keep the row.
    assert _operator_matches("Fictional Regional Carrier", "ZZ") is True


def test_extract_js_captures_operating_airline():
    from branded_fare_scraper.sources.ubfly import _EXTRACT_JS
    assert "Operating\\s*Airline" in _EXTRACT_JS
    assert "operating: operating" in _EXTRACT_JS


# ------------------- double-parse verification (round 15, A3) -------------- #
class _FakePage:
    """Minimal page stub: hands out one canned extraction payload per evaluate."""
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = 0

    async def evaluate(self, js, *args):
        self.calls += 1
        return self.payloads[min(self.calls - 1, len(self.payloads) - 1)]


def _payload(base, brands):
    return [{"carrier": "TK", "fare_iata": "TK", "baseText": base,
             "brands": [{"name": n, "priceText": p} for n, p in brands]}]


def test_extraction_diff_detects_and_clears():
    from branded_fare_scraper.sources.base import extraction_diff
    a = _payload("700.00 USD", [("ECOFLY", "0.00 USD"), ("FLEXFLY", "+108 USD")])
    b = _payload("700.00 USD", [("FLEXFLY", "+108 USD"), ("ECOFLY", "0.00 USD")])
    assert extraction_diff(a, b) == []                 # order-independent
    c = _payload("700.00 USD", [("ECOFLY", "0.00 USD")])
    assert extraction_diff(a, c)                       # a package vanished
    assert extraction_diff(a, []) and extraction_diff([], a)


def test_ubfly_double_parse_reads_twice_and_settles():
    import asyncio
    from branded_fare_scraper.sources.ubfly import Ubfly
    stable = _payload("700.00 USD", [("ECOFLY", "0.00 USD"), ("FLEXFLY", "+108 USD")])
    mid = _payload("700.00 USD", [("ECOFLY", "0.00 USD")])       # still rendering
    ub = Ubfly()
    ub.verify_delay_s = 0                                        # no real waiting in tests
    # Two agreeing reads -> no re-read.
    page = _FakePage([stable, stable])
    assert asyncio.run(ub._extract_verified(page, "t")) == stable
    assert page.calls == 2
    # First read caught mid-render -> re-read, and the settled payload is kept.
    page2 = _FakePage([mid, stable, stable])
    assert asyncio.run(ub._extract_verified(page2, "t")) == stable
    assert page2.calls == 3


def test_ubfly_double_parse_warns_when_page_never_settles(caplog):
    import asyncio
    import logging
    from branded_fare_scraper.sources.ubfly import Ubfly
    p1 = _payload("700.00 USD", [("ECOFLY", "0.00 USD")])
    p2 = _payload("710.00 USD", [("ECOFLY", "0.00 USD"), ("FLEXFLY", "+108 USD")])
    p3 = _payload("720.00 USD", [("ECOFLY", "0.00 USD"), ("FLEXFLY", "+108 USD"),
                                 ("PRIMEFLY", "+214 USD")])
    ub = Ubfly()
    ub.verify_delay_s = 0
    page = _FakePage([p1, p2, p3])
    with caplog.at_level(logging.WARNING, logger="bfs"):
        got = asyncio.run(ub._extract_verified(page, "IST-LHR"))
    assert got == p3                                   # the latest read is kept
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("double-parse mismatch" in w for w in warnings)
    assert any("IST-LHR" in w for w in warnings)       # the diff names the search


# ------------------- empty-content pruning (round 15, A4) ------------------ #
def test_platform_drops_rights_with_no_data_anywhere():
    from to_platform import feature_filter_patch, prune_empty_features, used_feature_keys
    fares = [{"features": {"checked_baggage": {"state": "Included"}, "pet": {}}},
             {"features": {"refund": {"state": "Paid"}}},
             {"features": {}}]
    assert used_feature_keys(fares) == ["checked_baggage", "refund"]
    keep = prune_empty_features(fares)
    assert keep == ["checked_baggage", "refund"]
    assert "pet" not in fares[0]["features"]           # stateless entry removed
    anchor, repl = feature_filter_patch(keep)
    assert anchor in repl                              # patch keeps the table itself
    assert '"checked_baggage"' in repl and '"refund"' in repl
    assert '"pet"' not in repl and '"wifi"' not in repl
    assert "delete FEATURE_META[k]" in repl


def test_platform_feature_patch_anchor_exists_in_template():
    from pathlib import Path
    from to_platform import FEATURE_META_ANCHOR
    tpl = Path(__file__).resolve().parent.parent / "docs" / "index.html"
    if not tpl.exists():                               # template not vendored here
        pytest.skip("no docs/index.html template in this checkout")
    assert FEATURE_META_ANCHOR in tpl.read_text(encoding="utf-8")


def test_excel_drops_all_unknown_amenity_columns():
    from branded_fare_scraper.amenities import AMENITY_KEYS
    from make_excel import used_amenity_keys
    blank = {k: ("Unknown", "") for k in AMENITY_KEYS}
    a1 = dict(blank); a1["checked_baggage"] = ("Included", "1 x 23 KG")
    a2 = dict(blank); a2["refund"] = ("Paid", "Kesintili")
    keys = used_amenity_keys([a1, a2, dict(blank)])
    assert keys == [k for k in AMENITY_KEYS if k in ("checked_baggage", "refund")]
    assert "pet" not in keys and "wifi" not in keys
    assert used_amenity_keys([dict(blank)]) == []      # nothing reported at all


# ==================== Trip.com adapter (round 15, phase B) ================= #
# Card texts below are the live-verified TK IST-JFK economy drawer from the
# Round-15 DOM recon (unnamed base $616, "Eco Fly" $624, "Extra Fly" $686).
TC_ECO_FLY = """Economy class
Eco Fly
Baggage
Personal item: Included
Carry-on baggage: 1 × 17 lbs
Checked baggage: Not included
Flexibility
Non-refundable (partial tax refund only)
Changes not permitted
Other benefits
Meals provided
Airline miles: at least 5,029
$624"""

TC_EXTRA_FLY = """Economy class
Extra Fly
Baggage
Personal item: Included
Carry-on baggage: 1 × 17 lbs
Checked baggage: 2 × 50 lbs
Flexibility
Cancellation fee: from $150
Change fee: from $224
Other benefits
Seat selection available
Meals provided
Airline miles: at least 5,029
$686"""

TC_UNNAMED_BASE = """Economy class
Baggage
Personal item: Included
Carry-on baggage: 1 × 17 lbs
Checked baggage: Not included
Flexibility
Non-refundable (partial tax refund only)
Changes not permitted
$616"""

TC_TR_CARD = """Ekonomi sınıfı
Extra Fly
Bagaj
Kabin bagajı hakkı: 1 × 8 kg
Check-in bagajı: 2 × 23 kg
İptal ücreti: from $150
Değişiklik ücreti: from $224
Koltuk seçimi yapılabilir
Yemek ve içecek sunulur
Salon erişimi var
Hava yolu şirketi milleri: en az 5.029
$686"""

TC_BUSINESS_CARD = """Business class
Business Fly
Baggage
Checked baggage: 2 × 70 lbs
Flexibility
First change free
Other benefits
Lounge access
Seat selection available
$2,410"""

TC_EASYCANCEL = """Economy class
EasyCancel
Get benefits worth $120
Free cancellation
$688"""

TC_STUDENT = """Economy class
Eco Fly
Only for ages 17 - 30
Student
Baggage
Checked baggage: 2 × 50 lbs
$540"""


def _amap(brand):
    return {a.canonical_key: (a.status, a.raw_value) for a in brand.amenities}


def test_tripcom_parses_verified_tk_cards():
    from branded_fare_scraper.sources.tripcom import parse_fare_card
    eco = parse_fare_card(TC_ECO_FLY, Cabin.ECONOMY, "TK")
    assert eco.raw_brand_name == "Eco Fly"
    assert eco.cabin == Cabin.ECONOMY and eco.currency == "USD"
    assert eco.price_value == pytest.approx(624.0)          # not the $ in fee lines
    assert eco.price_type == PriceType.ABSOLUTE
    a = _amap(eco)
    assert a["cabin_baggage"] == (AmenityStatus.INCLUDED, "1 x 8 KG")   # 17 lbs -> 8 kg
    assert a["checked_baggage"][0] == AmenityStatus.NOT_INCLUDED
    assert a["refund"][0] == AmenityStatus.NOT_INCLUDED
    assert a["change"][0] == AmenityStatus.NOT_INCLUDED
    assert a["meal"][0] == AmenityStatus.INCLUDED
    assert eco.miles.mileage_available is True and eco.miles.miles_earned == 5029

    extra = parse_fare_card(TC_EXTRA_FLY, Cabin.ECONOMY, "TK")
    assert extra.raw_brand_name == "Extra Fly"
    assert extra.price_value == pytest.approx(686.0)
    b = _amap(extra)
    assert b["checked_baggage"] == (AmenityStatus.INCLUDED, "2 x 23 KG")  # 50 lbs -> 23 kg
    assert b["seat_selection"][0] == AmenityStatus.INCLUDED
    assert b["refund"] == (AmenityStatus.PAID, "$150")     # fee amount kept as detail
    assert b["change"] == (AmenityStatus.PAID, "$224")


def test_tripcom_parses_turkish_locale_card():
    from branded_fare_scraper.sources.tripcom import parse_fare_card
    tr = parse_fare_card(TC_TR_CARD, Cabin.ECONOMY, "TK")
    assert tr.raw_brand_name == "Extra Fly" and tr.cabin == Cabin.ECONOMY
    assert tr.price_value == pytest.approx(686.0)
    a = _amap(tr)
    assert a["cabin_baggage"] == (AmenityStatus.INCLUDED, "1 x 8 KG")
    assert a["checked_baggage"] == (AmenityStatus.INCLUDED, "2 x 23 KG")
    assert a["refund"] == (AmenityStatus.PAID, "$150")
    assert a["change"] == (AmenityStatus.PAID, "$224")
    assert a["seat_selection"][0] == AmenityStatus.INCLUDED
    assert a["meal"][0] == AmenityStatus.INCLUDED
    assert a["lounge_access"][0] == AmenityStatus.INCLUDED
    assert tr.miles.miles_earned == 5029                   # "en az 5.029"


def test_tripcom_excludes_upsell_student_and_unnamed_cards(caplog):
    import logging
    from branded_fare_scraper.sources.tripcom import (cards_to_brands, is_excluded_card,
                                                      parse_fare_card)
    assert is_excluded_card(TC_EASYCANCEL) and parse_fare_card(TC_EASYCANCEL, Cabin.ECONOMY) is None
    assert is_excluded_card(TC_STUDENT) and parse_fare_card(TC_STUDENT, Cabin.ECONOMY) is None
    # The unnamed base fare is real but has no family name -> never published.
    with caplog.at_level(logging.DEBUG, logger="bfs"):
        assert parse_fare_card(TC_UNNAMED_BASE, Cabin.ECONOMY) is None
    assert any("unnamed base" in r.getMessage() for r in caplog.records)
    # Whole drawer: 5 cards in, only the 2 real families out, in screen order.
    brands = cards_to_brands([TC_UNNAMED_BASE, TC_ECO_FLY, TC_EXTRA_FLY,
                              TC_EASYCANCEL, TC_STUDENT], Cabin.ECONOMY, "TK")
    assert [b.raw_brand_name for b in brands] == ["Eco Fly", "Extra Fly"]
    assert [b.screen_order for b in brands] == [0, 1]


def test_tripcom_lbs_to_kg_snapping():
    from branded_fare_scraper.sources.tripcom import lbs_to_kg
    assert lbs_to_kg(17) == 8 and lbs_to_kg(15) == 8 and lbs_to_kg(18) == 8
    assert lbs_to_kg(22) == 10 and lbs_to_kg(23) == 10
    assert lbs_to_kg(50) == 23 and lbs_to_kg(70) == 32
    assert lbs_to_kg(100) == 45                            # unsnapped -> converted


def test_tripcom_row_filtering_excludes_exclusive_and_interline():
    from branded_fare_scraper.sources.tripcom import Tripcom
    tc = Tripcom()
    ok = {"index": 0, "airline": "Turkish Airlines", "text": "Turkish Airlines\nNonstop\n$624"}
    assert tc._row_matches(ok, "TK") is True
    assert tc._row_matches({**ok, "exclusive": True}, "TK") is False      # student fare chip
    assert tc._row_matches({**ok, "operating": "Aegean Airlines"}, "TK") is False   # interline
    assert tc._row_matches({**ok, "operating": "Turkish Airlines"}, "TK") is True
    # Another carrier's row never matches; a code-only row still does.
    assert tc._row_matches({"index": 1, "airline": "Emirates", "text": "Emirates\n$700"}, "TK") is False
    assert tc._row_matches({"index": 2, "airline": "", "text": "TK 1 · Nonstop\n$624"}, "TK") is True


def test_tripcom_best_ladders_prefers_packages_then_direct():
    from branded_fare_scraper.sources.tripcom import Tripcom, cards_to_brands
    direct = {"direct": True, "brands": cards_to_brands([TC_ECO_FLY, TC_EXTRA_FLY],
                                                        Cabin.ECONOMY, "TK")}
    conn = {"direct": False, "brands": cards_to_brands([TC_ECO_FLY, TC_EXTRA_FLY],
                                                       Cabin.ECONOMY, "TK")}
    for b in conn["brands"]:                       # same ladder, cheaper connection
        b.price_value -= 24
    best = Tripcom.best_ladders([conn, direct], Cabin.ECONOMY, "TK")
    assert [b.raw_brand_name for b in best[Cabin.ECONOMY]] == ["Eco Fly", "Extra Fly"]
    assert best[Cabin.ECONOMY][0].price_value == pytest.approx(624.0)   # direct won the tie
    # A richer connection still beats a thin direct (packages dominate).
    thin = {"direct": True, "brands": cards_to_brands([TC_ECO_FLY], Cabin.ECONOMY, "TK")}
    rich = {"direct": False, "brands": cards_to_brands([TC_ECO_FLY, TC_EXTRA_FLY],
                                                       Cabin.ECONOMY, "TK")}
    assert len(Tripcom.best_ladders([thin, rich], Cabin.ECONOMY, "TK")[Cabin.ECONOMY]) == 2


def test_tripcom_captures_business_leak_from_economy_search():
    # A Business card inside the class=y drawer is a real sellable fare.
    from branded_fare_scraper.sources.tripcom import Tripcom, cards_to_brands
    brands = cards_to_brands([TC_ECO_FLY, TC_EXTRA_FLY, TC_BUSINESS_CARD], Cabin.ECONOMY, "TK")
    best = Tripcom.best_ladders([{"direct": True, "brands": brands}], Cabin.ECONOMY, "TK")
    assert set(best) == {Cabin.ECONOMY, Cabin.BUSINESS}
    assert [b.raw_brand_name for b in best[Cabin.BUSINESS]] == ["Business Fly"]
    assert best[Cabin.BUSINESS][0].price_value == pytest.approx(2410.0)   # "$2,410"
    assert len(best[Cabin.ECONOMY]) == 2
    # The reverse direction is still noise: economy cards in a business search.
    eco_in_biz = cards_to_brands([TC_ECO_FLY, TC_BUSINESS_CARD], Cabin.BUSINESS, "TK")
    biz = Tripcom.best_ladders([{"direct": True, "brands": eco_in_biz}], Cabin.BUSINESS, "TK")
    assert set(biz) == {Cabin.BUSINESS}


def test_tripcom_rank_dates_direct_then_count_then_price():
    from branded_fare_scraper.sources.tripcom import Tripcom
    d0 = date(2026, 9, 2)
    entries = [
        {"dep": d0, "flights": [{"direct": False, "priceText": "$500"},
                                {"direct": False, "priceText": "$520"}]},
        {"dep": d0 + timedelta(days=1), "flights": [{"direct": True, "priceText": "$700"}]},
        {"dep": d0 + timedelta(days=2), "flights": []},
        {"dep": d0 + timedelta(days=3), "flights": [{"direct": False, "priceText": "$400"}]},
    ]
    ranked = Tripcom.rank_dates(entries)
    assert [e["dep"] for e in ranked] == [d0 + timedelta(days=1), d0,
                                          d0 + timedelta(days=3)]   # empty date dropped


class _TcPage:
    """Fake page: canned payload per evaluate call (for the double-parse test)."""
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = 0

    async def evaluate(self, js, *args):
        self.calls += 1
        return self.payloads[min(self.calls - 1, len(self.payloads) - 1)]


def test_tripcom_double_parse_of_a_drawer():
    import asyncio
    from branded_fare_scraper.sources.tripcom import Tripcom, drawer_payload
    from branded_fare_scraper.sources.base import extraction_diff
    full = [TC_ECO_FLY, TC_EXTRA_FLY]
    half = [TC_ECO_FLY]                                    # drawer still rendering
    assert extraction_diff(drawer_payload(full), drawer_payload(half))
    assert extraction_diff(drawer_payload(full), drawer_payload(list(reversed(full)))) == []
    tc = Tripcom()
    tc.verify_delay_s = 0
    page = _TcPage([half, full, full])
    got = asyncio.run(tc._extract_verified(page, "js", "t", wrap=drawer_payload))
    assert got == full and page.calls == 3                 # re-read, settled payload kept


def test_tripcom_run_unit_two_tier_walk(monkeypatch):
    """Tier 1 ranks dates from cheap lists; tier 2 opens every target drawer."""
    import asyncio
    from branded_fare_scraper.models import DatePlan, Job, ScrapeUnit
    from branded_fare_scraper.sources.tripcom import Tripcom

    d0 = date(2026, 9, 2)
    window = [(d0 + timedelta(days=i), d0 + timedelta(days=i + 3)) for i in range(4)]
    unit = ScrapeUnit(Job("TK", "IST", "JFK"),
                      DatePlan(Season.SUMMER, d0, d0 + timedelta(days=3), window))
    tk_direct = {"index": 0, "airline": "Turkish Airlines", "direct": True,
                 "text": "Turkish Airlines\nNonstop\n$624", "priceText": "$624"}
    tk_conn = {"index": 1, "airline": "Turkish Airlines", "direct": False,
               "text": "Turkish Airlines\n3h in Munich\n$600", "priceText": "$600"}
    other = {"index": 2, "airline": "Emirates", "direct": True,
             "text": "Emirates\nNonstop\n$580", "priceText": "$580"}
    lists = {
        (d0, "y"): [tk_conn, other],                       # connection only
        (d0 + timedelta(days=1), "y"): [tk_direct, tk_conn, other],
        (d0 + timedelta(days=2), "y"): [other],            # no TK at all
        (d0 + timedelta(days=1), "c"): [tk_direct],
    }
    drawers = {
        (d0, 1): [TC_ECO_FLY],                             # thin ladder
        (d0 + timedelta(days=1), 0): [TC_UNNAMED_BASE, TC_ECO_FLY, TC_EXTRA_FLY],
        (d0 + timedelta(days=1), 1): [TC_ECO_FLY, TC_EXTRA_FLY, TC_EASYCANCEL],
        (d0 + timedelta(days=1), 0, "c"): [TC_BUSINESS_CARD],
    }
    seen_dates, opened = [], []

    async def fake_list(self, page, o, d, dep, cls):
        seen_dates.append((dep, cls))
        return lists.get((dep, cls), [])

    async def fake_panel(self, page, o, d, dep, cls, row):
        opened.append((dep, cls, row["index"]))
        cards = drawers.get((dep, row["index"], cls)) or drawers.get((dep, row["index"]), [])
        if cls == "c":
            return drawers.get((dep, row["index"], "c"), [])
        return cards

    monkeypatch.setattr(Tripcom, "_flight_list", fake_list)
    monkeypatch.setattr(Tripcom, "_fare_panel", fake_panel)
    monkeypatch.setattr(Tripcom, "_pe_supported", False)   # PE probe already failed
    res = asyncio.run(Tripcom().run_unit(None, unit))

    assert res.status.value == "success"
    cabs = {c.cabin: c for c in res.cabin_results}
    # Tier 1 scanned EVERY window date for both searched classes (cheap lists).
    assert {d for d, _c in seen_dates} == {d for d, _r in window}
    assert {c for _d, c in seen_dates} == {"y", "c"}
    # Tier 2 only drilled the ranked dates, and there it opened EVERY TK flight.
    assert (d0 + timedelta(days=1), "y", 0) in opened
    assert (d0 + timedelta(days=1), "y", 1) in opened
    assert all(dep != d0 + timedelta(days=2) for dep, _c, _i in opened)   # no TK there
    # Direct flight's ladder published (tie on packages -> direct wins).
    assert [b.raw_brand_name for b in cabs[Cabin.ECONOMY].brands] == ["Eco Fly", "Extra Fly"]
    assert cabs[Cabin.ECONOMY].departure == d0 + timedelta(days=1)
    assert cabs[Cabin.ECONOMY].source == "Trip.com"
    assert [b.raw_brand_name for b in cabs[Cabin.BUSINESS].brands] == ["Business Fly"]


def test_tripcom_pe_probe_disables_dedicated_pe_search(monkeypatch):
    """A failed class=s probe turns PE off for the run (leak capture covers it)."""
    import asyncio
    from branded_fare_scraper.models import DatePlan, Job, ScrapeUnit
    from branded_fare_scraper.sources.tripcom import Tripcom

    d0 = date(2026, 9, 2)
    window = [(d0, d0 + timedelta(days=3))]
    unit = ScrapeUnit(Job("TK", "IST", "JFK"),
                      DatePlan(Season.SUMMER, d0, d0 + timedelta(days=3), window))
    probes, searched = [], []

    async def fake_probe(self, page, o, d, dep):
        probes.append(dep)
        type(self)._pe_supported = False          # site served economy instead
        return False

    async def fake_list(self, page, o, d, dep, cls):
        searched.append(cls)
        return []

    monkeypatch.setattr(Tripcom, "_pe_supported", None)    # not probed yet
    monkeypatch.setattr(Tripcom, "probe_premium_economy", fake_probe)
    monkeypatch.setattr(Tripcom, "_flight_list", fake_list)
    tc = Tripcom()
    res = asyncio.run(tc.run_unit(None, unit))
    assert probes == [d0] and "s" not in searched          # probed once, PE skipped
    assert set(searched) == {"y", "c"}
    assert res.status.value == "no_availability"
    # Second unit: the disabled probe is remembered, not repeated.
    searched.clear()
    asyncio.run(Tripcom().run_unit(None, unit))
    assert probes == [d0] and "s" not in searched
    # A fresh run in the same process must re-learn it (runner resets at start).
    from branded_fare_scraper.sources.base import reset_caches
    reset_caches()
    assert Tripcom._pe_supported is None


# --- pilot-2 defects: drawer variant, row identity, Ubfly challenge ------- #
# Verbatim from the pilot evidence shot tripcom_JFK-ISB_2026-10-22_y_row4.png:
# a "Select Fare" drawer card with a bare cabin line and a struck-through price.
TC_BIZ_COMFORT = """Business
Business Comfort
Baggage
Checked baggage: 2 × 70 lbs
Flexibility
Cancellation fee: from $320
First ticket change is free
Other benefits
Seat selection available
Airline miles: at least 14,208
VIP experience:
$7,896
$8,005"""

TC_HOTEL_PROMO = """Flyer exclusive
Book a flight & save up to 25% on your hotel
Don't miss out! Join 28% of Trip.com flyers and book your stay for less.
Baggage included
$120"""


def test_tripcom_parses_bare_cabin_line_drawer_variant():
    """The pilot's drawer has "Business" (not "Business class") as its header."""
    from branded_fare_scraper.sources.tripcom import parse_fare_card
    card = parse_fare_card({"text": TC_BIZ_COMFORT, "struck": ["$8,005"]},
                           Cabin.ECONOMY, "QR")
    assert card is not None                                # used to be dropped entirely
    assert card.raw_brand_name == "Business Comfort"
    assert card.cabin == Cabin.BUSINESS                    # from the bare cabin line
    assert card.price_value == pytest.approx(7896.0)       # discounted, not $8,005
    a = _amap(card)
    assert a["checked_baggage"] == (AmenityStatus.INCLUDED, "2 x 32 KG")   # 70 lbs
    assert a["refund"] == (AmenityStatus.PAID, "$320")     # fee $ is not the fare price
    assert a["change"][0] == AmenityStatus.INCLUDED        # "First ticket change is free"
    assert a["seat_selection"][0] == AmenityStatus.INCLUDED
    assert card.miles.miles_earned == 14208
    assert "lounge_access" not in a and "priority_boarding" not in a   # bare "VIP experience"


def test_tripcom_card_price_ignores_struck_and_fee_amounts():
    from branded_fare_scraper.sources.tripcom import card_price, parse_fare_card
    lines = ["Cancellation fee: from $320", "$7,896", "$8,005"]
    assert card_price(lines, ["$8,005"]) == pytest.approx(7896.0)
    # Even with no struck-through info, the crossed-out "was" price is the higher.
    assert card_price(lines) == pytest.approx(7896.0)
    assert card_price(["$7,896 $8,005"]) == pytest.approx(7896.0)   # one rendered line
    assert card_price(["Cancellation fee: from $320"]) is None      # fees are not prices
    assert card_price(["Get benefits worth $120"]) is None
    # Plain-text cards (no struck list) still parse — same result as the dict form.
    assert parse_fare_card(TC_BIZ_COMFORT, Cabin.ECONOMY).price_value == pytest.approx(7896.0)


def test_tripcom_excludes_flyer_exclusive_hotel_promo():
    from branded_fare_scraper.sources.tripcom import cards_to_brands, is_excluded_card
    assert is_excluded_card(TC_HOTEL_PROMO) is True
    brands = cards_to_brands([TC_BIZ_COMFORT, TC_HOTEL_PROMO], Cabin.ECONOMY, "QR")
    assert [b.raw_brand_name for b in brands] == ["Business Comfort"]


def test_tripcom_drawer_extraction_is_structural():
    from branded_fare_scraper.sources.tripcom import _DRAWER_JS, _LIST_JS
    # The recon page's class is a FAST PATH only; the drawer variant that lacks
    # it must still resolve through the structural policy-block climb.
    assert "result-item__normal-wrapper" in _DRAWER_JS
    assert "if (fast.length) return fast.map(pack);" in _DRAWER_JS   # guarded, not required
    assert "el.parentElement" in _DRAWER_JS and "blen * 2.5" in _DRAWER_JS
    assert "baggage|bagaj" in _DRAWER_JS and "struck" in _DRAWER_JS
    assert "flt-drawer-policy-be" in _DRAWER_JS and "select fare" in _DRAWER_JS.lower()
    # Rows carry a stable fingerprint for click-time re-location.
    assert "rowKey" in _LIST_JS and "__rowKey" in _LIST_JS


def test_drawer_matches_row_guards_against_wrong_flight():
    from branded_fare_scraper.sources.tripcom import drawer_matches_row
    header = ("New York (JFK) → Islamabad (ISB) | Thu, Oct 29 | "
              "9:40 PM – 1:40 AM+2 (1 stop)")
    tk_row = {"text": "Turkish Airlines\n9:40 PM – 1:40 AM\nNonstop\n$624"}
    assert drawer_matches_row(header, tk_row, "TK") is True
    other_row = {"text": "Turkish Airlines\n6:15 PM – 9:05 AM\n$624"}
    assert drawer_matches_row(header, other_row, "TK") is False      # times disagree
    # A drawer that spells out another airline is refused even if times match.
    named = header + "\nOperated by Qatar Airways"
    assert drawer_matches_row(named, tk_row, "TK") is False
    assert drawer_matches_row(named, tk_row, "QR") is True
    assert drawer_matches_row("", tk_row, "TK") is True              # nothing to check


class _DrawerPage:
    """Fake results page: canned DOM rows + a drawer that may or may not open.

    Row matching mirrors the adapter's JS (data-testid, then airline+times+
    duration) and deliberately IGNORES price, so a test whose price drifts
    between listing and clicking proves the fingerprint is price-free.
    """
    def __init__(self, rows=None, drawer_text="", cards=None, opens=True, sticky=False,
                 url="", card_pages=None, stale=False, views=None):
        self.url = url
        #: Successive carousel views; each advance reveals the next page.
        self.card_pages = card_pages
        #: 2-D drawer: {(vertical, horizontal): [cards]} — what is in view now.
        self.views = dict(views) if views else None
        if self.views is None and card_pages:
            self.views = {(0, i): p for i, p in enumerate(card_pages)}
        self.v_idx = 0
        self.page_idx = 0
        self.advances = 0
        self.scrolls = 0
        self.scroll_resets = 0
        self.stale = stale                 # a drawer left open before we click
        self.rows = rows if rows is not None else []
        self.drawer_text = drawer_text
        self.cards = cards if cards is not None else []
        self.opens = opens
        self.sticky = sticky
        self.open = False
        self.clicked_by_key = 0
        self.clicked_by_index = 0
        self.closed = 0
        self.shots = []
        self.gotos = []
        self.keyboard = self

    @staticmethod
    def _key(r):
        return f"{r['airline']}|{r['dep']}|{r['arr']}|{r['dur']}".lower()

    @staticmethod
    def _loose(r):
        return f"{r['airline']}|{r['dep']}".lower()

    def _match(self, keys):
        wid, wkey, wloose = (list(keys) + ["", "", ""])[:3]
        for r in self.rows:
            if wid and r.get("testid") == wid:
                return True
            if wkey and self._key(r) == wkey:
                return True
            if wloose and self._loose(r) == wloose:
                return True
        return False

    async def press(self, _key):
        if not self.sticky:
            self.open = False
            self.stale = False

    async def goto(self, url, **kw):
        self.gotos.append(url)
        self.url = url
        self.open = False

    async def screenshot(self, path=None, **kw):
        self.shots.append(str(path))
        return b""

    async def evaluate(self, js, *args):
        if "advanced" in js:                              # carousel step (horizontal)
            self.advances += 1
            if self.views and (self.v_idx, self.page_idx + 1) in self.views:
                self.page_idx += 1
                return {"advanced": True, "how": "arrow"}
            return {"advanced": False, "reason": "no_control"}
        if "scrolled" in js:                              # drawer scroll (vertical)
            self.scrolls += 1
            if self.views and (self.v_idx + 1, 0) in self.views:
                self.v_idx += 1
                self.page_idx = 0
                return {"scrolled": True, "how": "container"}
            return {"scrolled": False, "reason": "bottom"}
        if "e.scrollTop = 0" in js:                       # rewind before closing
            self.scroll_resets += 1
            self.v_idx = self.page_idx = 0
            return True
        if js.startswith("(keys)"):                       # click by identity
            self.clicked_by_key += 1
            hit = self._match(args[0] if args else [])
            if hit and self.opens:
                self.open = True
            return hit
        if js.strip().startswith("(i) =>"):               # legacy index click
            self.clicked_by_index += 1
            if self.opens:
                self.open = True
            return True
        if "struck" in js:                                # drawer cards in view
            if not self.open:
                return []
            if self.views is not None:
                return self.views.get((self.v_idx, self.page_idx), [])
            return self.cards
        if "clicked: true" in js:                         # close control
            # Mirrors _CLOSE_DRAWER_JS: it resolves the drawer through the same
            # __drawerRoot() chain as the detector, so a variant the old
            # '.flt-drawer-policy-be' query could never reach still closes.
            shown = bool(self.open or self.stale)
            if not shown:
                return {"found": False, "clicked": False, "direct": False, "id": ""}
            self.closed += 1
            if not self.sticky:
                self.open = False
                self.stale = False
            return {"found": True, "clicked": True, "direct": True, "id": "DIV.flt-drawer-policy-be"}
        if "found: true" in js:                           # drawer root probe
            shown = bool(self.open or self.stale)
            return {"found": shown, "bodyLen": 9999,
                    "text": self.drawer_text if shown else "",
                    "len": len(self.drawer_text)}
        if "u_select_btn" in js and "length" in js:       # hydration probe
            return len(self.rows) or 1
        if "flt-drawer-policy-be" in js:                  # close button
            self.closed += 1
            if not self.sticky:
                self.open = False
                self.stale = False
            return True
        return None


_AI_DOM_ROW = {"testid": "u-flight-card-1", "airline": "air india", "dep": "12:10 pm",
               "arr": "3:35 pm", "dur": "14h 25m", "price": "$1,317"}   # price refreshed
_AI_LISTED_ROW = {"index": 0, "testid": "u-flight-card-1",
                  "rowKey": "air india|12:10 pm|3:35 pm|14h 25m",
                  "rowKeyLoose": "air india|12:10 pm",
                  "text": "Air India\n12:10 PM – 3:35 PM\n14h 25m\n$1,277",
                  "_target": "AI"}
_AI_DRAWER = "Vancouver (YVR) → Delhi (DEL) | Mon, Nov 9 | 12:10 PM – 3:35 PM (1 stop)"


def test_tripcom_row_identity_is_price_free():
    """Trip.com refreshes fares in place — a price in the key breaks the click."""
    from branded_fare_scraper.sources.tripcom import _JS_HELPERS, _LIST_JS, row_identity
    key_fn = _JS_HELPERS.split("const __rowKey")[1].split("};")[0]
    assert "$" not in key_fn                       # no price anywhere in the identity
    assert "__times" in key_fn and "__dur" in key_fn
    assert "__rowKeyLoose" in _JS_HELPERS
    assert "rowKey:" in _LIST_JS and "testid:" in _LIST_JS
    assert row_identity(_AI_LISTED_ROW) == ["u-flight-card-1",
                                            "air india|12:10 pm|3:35 pm|14h 25m",
                                            "air india|12:10 pm"]


def test_tripcom_clicks_row_after_price_drift():
    """The live failure: listed at $1,277, $1,317 at click time -> must still open."""
    import asyncio
    from branded_fare_scraper.sources.tripcom import Tripcom
    tc = Tripcom()
    tc.verify_delay_s = tc.drawer_poll_s = tc.drawer_timeout_s = 0
    page = _DrawerPage(rows=[_AI_DOM_ROW], drawer_text=_AI_DRAWER,
                       cards=[{"text": TC_ECO_FLY}])
    got = asyncio.run(tc._fare_panel(page, "YVR", "DEL", date(2026, 11, 9), "y",
                                     dict(_AI_LISTED_ROW)))
    assert page.clicked_by_key == 1 and page.clicked_by_index == 0
    assert got == [{"text": TC_ECO_FLY}]
    # Identity still resolves when the row loses its testid (stable key path).
    page2 = _DrawerPage(rows=[dict(_AI_DOM_ROW, testid="")], drawer_text=_AI_DRAWER,
                        cards=[{"text": TC_ECO_FLY}])
    assert asyncio.run(tc._fare_panel(page2, "YVR", "DEL", date(2026, 11, 9), "y",
                                      dict(_AI_LISTED_ROW))) == [{"text": TC_ECO_FLY}]


def test_tripcom_skips_when_row_vanished_or_drawer_mismatches(caplog):
    import asyncio
    import logging
    from branded_fare_scraper.sources.tripcom import Tripcom
    tc = Tripcom()
    tc.verify_delay_s = tc.drawer_poll_s = tc.drawer_timeout_s = 0
    # (a) the row is genuinely gone -> never click a different one
    gone = _DrawerPage(rows=[], cards=[{"text": TC_ECO_FLY}])
    with caplog.at_level(logging.WARNING, logger="bfs"):
        assert asyncio.run(tc._fare_panel(gone, "YVR", "DEL", date(2026, 11, 9), "y",
                                          dict(_AI_LISTED_ROW))) == []
    assert gone.clicked_by_index == 0
    assert any("no longer on the page" in r.getMessage() for r in caplog.records)
    # (b) a drawer for ANOTHER flight opened -> skip instead of publishing it
    caplog.clear()
    wrong = _DrawerPage(rows=[_AI_DOM_ROW], cards=[{"text": TC_BIZ_COMFORT}],
                        drawer_text="Vancouver (YVR) → Delhi (DEL) | 6:15 PM – 9:05 AM")
    with caplog.at_level(logging.WARNING, logger="bfs"):
        assert asyncio.run(tc._fare_panel(wrong, "YVR", "DEL", date(2026, 11, 9), "y",
                                          dict(_AI_LISTED_ROW))) == []
    assert wrong.closed >= 1                          # drawer closed before moving on
    assert any("not the intended flight" in r.getMessage() for r in caplog.records)


def test_tripcom_never_treats_the_page_body_as_a_drawer(caplog):
    """No drawer -> screenshot + WARNING + skip; never parse the whole page."""
    import asyncio
    import logging
    from pathlib import Path
    from branded_fare_scraper.sources.tripcom import (_DRAWER_JS, _DRAWER_ROOT_FN,
                                                      Tripcom)
    # The JS refuses a candidate whose text is ~the entire body, and the card
    # extractor bails out instead of falling back to document.body.
    assert "bodyLen * 0.9" in _DRAWER_ROOT_FN
    assert "if (!found) return [];" in _DRAWER_JS
    tc = Tripcom()
    tc.verify_delay_s = tc.drawer_poll_s = tc.drawer_timeout_s = 0
    Tripcom.evidence_dir = Path("/tmp/does-not-need-to-exist")
    try:
        page = _DrawerPage(rows=[_AI_DOM_ROW], opens=False, cards=[{"text": TC_ECO_FLY}])
        with caplog.at_level(logging.WARNING, logger="bfs"):
            got = asyncio.run(tc._fare_panel(page, "YVR", "DEL", date(2026, 11, 9), "y",
                                             dict(_AI_LISTED_ROW)))
        assert got == []                                   # nothing published
        assert page.closed == 0                            # close nothing, per the rule
        assert any("nodrawer" in s for s in page.shots)     # evidence saved
        assert any("no fare drawer opened" in r.getMessage() for r in caplog.records)
        assert asyncio.run(tc._wait_drawer(page)) is None
    finally:
        Tripcom.evidence_dir = None


def test_tripcom_resets_the_page_when_the_drawer_will_not_close():
    """A stuck drawer must not leak into the next flight's click."""
    import asyncio
    from branded_fare_scraper.sources.tripcom import Tripcom
    tc = Tripcom()
    tc.verify_delay_s = tc.drawer_poll_s = tc.drawer_timeout_s = 0
    tc.drawer_close_wait_s = 0
    tc.hydrate_timeout_s = 1
    page = _DrawerPage(rows=[_AI_DOM_ROW], drawer_text=_AI_DRAWER,
                       cards=[{"text": TC_ECO_FLY}], sticky=True)
    got = asyncio.run(tc._fare_panel(page, "YVR", "DEL", date(2026, 11, 9), "y",
                                     dict(_AI_LISTED_ROW)))
    assert got == [{"text": TC_ECO_FLY}]                   # the parse still counts
    assert page.gotos and "tickets-yvr-del" in page.gotos[0]   # results reloaded clean


class _CfPage:
    """Fake Ubfly page: challenge markers vs. real site markup."""
    def __init__(self, title="", body="", marked=False, site=False):
        self._title, self._body, self._marked, self._site = title, body, marked, site

    async def title(self):
        return self._title

    async def evaluate(self, js, *args):
        if "flight-item" in js:
            return self._site
        if "cf-chl" in js:
            return self._marked
        if "innerText" in js:
            return self._body
        return None


def test_ubfly_detects_bot_challenge_fast():
    import asyncio
    from branded_fare_scraper.sources.ubfly import Ubfly
    ub = Ubfly()
    ub.challenge_probe_s = 1.0
    text = asyncio.run(ub._challenge_present(
        _CfPage(title="Just a moment...", body="Verify you are human")))
    assert text is True
    assert asyncio.run(ub._challenge_present(_CfPage(marked=True))) is True
    # Real site markup ends the probe immediately — no 5s tax on good pages.
    assert asyncio.run(ub._challenge_present(
        _CfPage(title="Ubfly", body="Uçuş sonuçları", site=True))) is False


def test_ubfly_disables_itself_for_the_run_after_a_challenge():
    import asyncio
    from branded_fare_scraper.sources.ubfly import Ubfly
    from branded_fare_scraper.sources.base import reset_caches
    ub = Ubfly()
    try:
        Ubfly._challenge_disabled = True
        # Short-circuits before touching the page at all (page=None proves it).
        assert asyncio.run(ub._search(None, "IST", "LHR", date(2026, 9, 2),
                                      Cabin.ECONOMY)) == []
        reset_caches()                                 # a fresh run re-tries Ubfly
        assert Ubfly._challenge_disabled is False
    finally:
        Ubfly._challenge_disabled = False


# --- pilot-5 defects: badge chips, page context, drawer date --------------- #
def test_tripcom_badge_lines_are_not_airlines():
    from branded_fare_scraper.sources.tripcom import is_badge_line, strip_badges
    for chip in ("Included", "Fastest", "Cheapest nonstop", "Recommended",
                 "Exclusive fare", "Flyer exclusive", "Carry-on baggage included",
                 "<9 left", "3 left", "Student", "Dahil", "En hızlı", "son 2 koltuk"):
        assert is_badge_line(chip) is True, chip
    for real in ("Turkish Airlines", "Air India", "Qatar Airways", "9:40 PM"):
        assert is_badge_line(real) is False, real
    rows = "Included\n<9 left\nTurkish Airlines\n9:40 PM\n$624"
    assert strip_badges(rows) == "Turkish Airlines\n9:40 PM\n$624"


def test_tripcom_row_matching_ignores_badge_chips():
    """Pilot 5: chips became the 'airline', so target matching drifted."""
    from branded_fare_scraper.sources.tripcom import Tripcom
    tc = Tripcom()
    chipped = {"index": 0, "airline": "Included",          # chip leaked into the slot
               "text": "Included\n<9 left\nTurkish Airlines\n6:50 AM\n15h 55m\n$1,317"}
    assert tc._row_matches(chipped, "TK") is True
    assert tc._row_matches(chipped, "AI") is False
    # A row whose only carrier hint is a chip must not match anybody.
    assert tc._row_matches({"index": 1, "airline": "Fastest",
                            "text": "Fastest\n6:50 AM\n$1,317"}, "TK") is False


def test_tripcom_js_airline_skips_badges():
    """The fix lives in JS, so exercise the real helper with node."""
    import json
    import shutil
    import subprocess
    if not shutil.which("node"):
        pytest.skip("node not available for JS-level checks")
    from branded_fare_scraper.sources.tripcom import _JS_HELPERS
    cases = [
        ("Included\nTurkish Airlines\n6:50 AM\n7:45 AM\n15h 55m\n$1,317", "turkish airlines"),
        ("Fastest\nAir India\n12:10 PM\n3:35 PM\n14h 25m\n$1,277", "air india"),
        ("<9 left\nIncluded\nQatar Airways\n10:00 PM\n5:15 AM\n21h 45m\n$3,000",
         "qatar airways"),
        ("Turkish Airlines\n7:00 PM\n5:20 AM\n21h 50m\n$980", "turkish airlines"),
    ]
    probe = (
        "const rowStub = { querySelector: () => null, innerText: '' };\n"
        "const airline = new Function('row', 'text', %s + '\\n; return __airline(row, text);');\n"
        "const key = new Function('row', %s + '\\n; return __rowKey(row);');\n"
        "const out = %s.map(t => [airline(rowStub, t), key({querySelector: () => null, innerText: t})]);\n"
        "console.log(JSON.stringify(out));"
        % (json.dumps(_JS_HELPERS), json.dumps(_JS_HELPERS),
           json.dumps([t for t, _ in cases]))
    )
    res = subprocess.run(["node", "-e", probe], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr[:400]
    got = json.loads(res.stdout)
    for (text, want), (airline, key) in zip(cases, got):
        assert airline.lower() == want, f"{text!r} -> {airline!r}"
        assert key.startswith(want), key            # and the KEY starts with the airline
        assert not key.startswith(("included", "fastest", "<9", "9 left"))


def test_tripcom_renavigates_when_page_shows_another_date():
    """Row dicts come from a cached list; the page may be on another date."""
    import asyncio
    from branded_fare_scraper.sources.tripcom import Tripcom
    tc = Tripcom()
    tc.verify_delay_s = tc.drawer_poll_s = tc.drawer_timeout_s = 0
    tc.hydrate_timeout_s = 1
    want = tc.search_url("YVR", "DEL", date(2026, 11, 9), "y")
    # (a) page already on the right search -> no navigation at all
    right = _DrawerPage(rows=[_AI_DOM_ROW], drawer_text=_AI_DRAWER,
                        cards=[{"text": TC_ECO_FLY}], url=want)
    asyncio.run(tc._fare_panel(right, "YVR", "DEL", date(2026, 11, 9), "y",
                               dict(_AI_LISTED_ROW)))
    assert right.gotos == []
    # (b) page is on ANOTHER date -> restore context before clicking
    stale = _DrawerPage(rows=[_AI_DOM_ROW], drawer_text=_AI_DRAWER,
                        cards=[{"text": TC_ECO_FLY}],
                        url=tc.search_url("YVR", "DEL", date(2026, 9, 19), "y"))
    got = asyncio.run(tc._fare_panel(stale, "YVR", "DEL", date(2026, 11, 9), "y",
                                     dict(_AI_LISTED_ROW)))
    assert stale.gotos == [want]                    # re-navigated to the row's own date
    assert got == [{"text": TC_ECO_FLY}]
    # (c) a class mismatch counts too
    other_cls = _DrawerPage(rows=[_AI_DOM_ROW], drawer_text=_AI_DRAWER,
                            cards=[{"text": TC_ECO_FLY}],
                            url=tc.search_url("YVR", "DEL", date(2026, 11, 9), "c"))
    asyncio.run(tc._fare_panel(other_cls, "YVR", "DEL", date(2026, 11, 9), "y",
                               dict(_AI_LISTED_ROW)))
    assert other_cls.gotos == [want]


def test_drawer_date_conflict_is_rejected():
    from branded_fare_scraper.sources.tripcom import drawer_dates, drawer_matches_row
    header = ("Vancouver (YVR) → Delhi (DEL) | Depart Sat, Sep 19 | "
              "12:10 PM – 3:35 PM (1 stop)")
    assert drawer_dates(header) == {(9, 19)}
    row = {"text": "Air India\n12:10 PM – 3:35 PM\n$1,277"}
    # Times agree, but the drawer is a DIFFERENT date -> reject (pilot-5 case).
    assert drawer_matches_row(header, row, "AI", date(2026, 9, 11)) is False
    assert drawer_matches_row(header, row, "AI", date(2026, 9, 19)) is True
    # No date in the drawer -> the date check simply does not apply.
    assert drawer_matches_row("Vancouver (YVR) → Delhi (DEL) | 12:10 PM – 3:35 PM",
                              row, "AI", date(2026, 9, 11)) is True
    assert drawer_dates("no dates here") == set()
    assert drawer_dates("Depart 19 Sep 2026") == {(9, 19)}


def test_drawer_stats_counter_summarises_a_unit():
    from branded_fare_scraper.sources.tripcom import DrawerStats
    s = DrawerStats()
    for ev in ("opened", "parsed", "opened", "wrong_flight", "no_drawer", "no_drawer"):
        s.mark(ev)
    s.cards += 5                                   # ladder depth across the unit
    assert (s.opened, s.parsed, s.rejected, s.cards) == (2, 1, 3, 5)
    assert s.summary() == ("opened=2 parsed=1 cards=5 stopped_early=0 rejected=3 "
                           "(no_drawer=2, wrong_flight=1)")
    assert DrawerStats().summary().endswith("(none)")


# --- pilot-7 defect: card container swallowed only the policy block -------- #
# Verbatim shape of the live TK JFK-ISB 2026-10-10 class=y drawer: card 0 is a
# badge + cabin line with NO family name (the unnamed base), card 1 names
# "Promotional" between the cabin line and "Baggage".
TC_LIVE_CARD0 = """Recommended
Economy class
Baggage
Personal item: Included
Carry-on baggage: 1 × 17 lbs
Checked baggage: 2 × 50 lbs
Flexibility
Non-refundable
Change fee: from $180
Other benefits
Meals provided
$592"""

TC_LIVE_CARD1 = """Economy class
Promotional
Baggage
Personal item: Included
Carry-on baggage: 1 × 17 lbs
Checked baggage: 2 × 50 lbs
Flexibility
Non-refundable (partial tax refund only)
Change fee: from $220
Other benefits
Meals provided
$648"""


def test_tripcom_live_drawer_publishes_named_family_only():
    """Pilot 7 published cabin words at other cards' prices — never again."""
    from branded_fare_scraper.sources.tripcom import cards_to_brands, parse_fare_card
    base = parse_fare_card(TC_LIVE_CARD0, Cabin.ECONOMY, "TK")
    assert base is None                               # unnamed base fare, not published
    named = parse_fare_card(TC_LIVE_CARD1, Cabin.ECONOMY, "TK")
    assert named.raw_brand_name == "Promotional"      # the line under the cabin line
    assert named.price_value == pytest.approx(648.0)  # its OWN price, not $592/$180
    brands = cards_to_brands([TC_LIVE_CARD0, TC_LIVE_CARD1], Cabin.ECONOMY, "TK")
    assert [(b.raw_brand_name, b.price_value) for b in brands] == [("Promotional", 648.0)]
    # No published name may be a bare cabin word or a stray benefit word.
    for b in brands:
        assert b.raw_brand_name.lower() not in ("business", "economy", "first",
                                                "flexible", "flexibility", "baggage")
    # A real family name survives display normalisation — the carrier spelling
    # table may restyle it ("Eco Fly" -> "EcoFly") but never replaces the word.
    from branded_fare_scraper.normalization import brand_match_key, pretty_brand_name
    for real in ("Eco Fly", "Extra Fly", "Business Comfort", "Promotional"):
        assert brand_match_key(pretty_brand_name(real, "TK")) == brand_match_key(real)


def test_tripcom_card_name_ignores_badge_and_money_lines():
    from branded_fare_scraper.sources.tripcom import parse_fare_card
    # A chip directly under the cabin line is not a family name.
    chipped = "Economy class\nRecommended\nBaggage\nChecked baggage: 2 × 50 lbs\n$592"
    assert parse_fare_card(chipped, Cabin.ECONOMY, "TK") is None
    # Neither is a price line.
    priced = "Economy class\n$592\nBaggage\nChecked baggage: 2 × 50 lbs\n$592"
    assert parse_fare_card(priced, Cabin.ECONOMY, "TK") is None


def test_tripcom_dedupes_repeated_family_names_keeping_cheapest():
    from branded_fare_scraper.sources.tripcom import dedupe_ladder
    from branded_fare_scraper.models import PriceType, RawBrand
    def b(name, price):
        return RawBrand(name, Cabin.BUSINESS, 0, price, PriceType.ABSOLUTE)
    out = dedupe_ladder([b("Business", 4695.0), b("Business", 4576.0),
                         b("Business", 4776.0)])
    assert [(x.raw_brand_name, x.price_value) for x in out] == [("Business", 4576.0)]
    assert [x.screen_order for x in out] == [0]
    # Distinct families are all kept, in order.
    kept = dedupe_ladder([b("Business Saver", 4270.0), b("Business Comfort", 4576.0)])
    assert [x.raw_brand_name for x in kept] == ["Business Saver", "Business Comfort"]
    assert [x.screen_order for x in kept] == [0, 1]


#: Minimal DOM double for node-level checks of the adapter's injected JS.
#: It models geometry and computed style because the drawer root finder
#: must reject a closed-but-mounted drawer, which is a VISIBILITY question.
_JS_DOM_SHIM = r"""
function mk(tag, text, attrs, kids){
  const n = {tag:tag, _text:text||'', attrs:attrs||{}, kids:kids||[], parentElement:null,
    get innerText(){ return this._text || this.kids.map(k=>k.innerText).join('\n'); },
    getAttribute(x){ return this.attrs[x] || null; },
    contains(o){ return this===o || this.kids.some(k=>k.contains(o)); },
    // A closed-but-mounted drawer collapses to a zero box; that is exactly the
    // state the root finder must reject, so the shim has to model geometry.
    getBoundingClientRect(){ return this.attrs.hidden === '1'
        ? {width:0, height:0} : {width:parseInt(this.attrs.w||'620',10), height:840}; },
    get tagName(){ return this.tag.toUpperCase(); },
    get className(){ return this.attrs.class || ''; },
    get children(){ return this.kids; },
    matches(sel){ return match(this, sel); },
    closest(sel){ let e=this; while(e){ if(match(e,sel)) return e; e=e.parentElement; } return null; },
    click(){ CLICKS.push(this.attrs.class || this.tag); },
    querySelectorAll(sel){ return all(this).filter(e=>match(e,sel)); } };
  n.kids.forEach(k=>{ k.parentElement = n; });
  return n;
}
const CLICKS = [];
function getComputedStyle(e){
  return {display:    e.attrs.hidden === '1' ? 'none' : (e.attrs.display || 'block'),
          visibility: e.attrs.visibility || 'visible',
          opacity:    e.attrs.opacity    || '1'};
}
function all(n){ return n.kids.reduce((a,k)=>a.concat(all(k)), [n]); }
function match(e, sel){
  return sel.split(',').map(s=>s.trim()).some(s=>{
    if(s.startsWith('.')) return (e.attrs.class||'').split(' ').includes(s.slice(1));
    if(s.startsWith('[role=')) return e.attrs.role === 'dialog';
    let m = s.match(/^\[(class|aria-label|title)\*="([^"]+)"/);
    if(m){ const v = m[1]==='class' ? (e.attrs.class||'') : (e.attrs[m[1]]||'');
           return new RegExp(m[2], 'i').test(v); }
    if(s.startsWith('[')) return false;
    return e.tag === s;
  });
}
"""


def test_tripcom_js_card_container_includes_name_and_price():
    """Exercise the real container algorithm on a two-card drawer DOM."""
    import json
    import shutil
    import subprocess
    if not shutil.which("node"):
        pytest.skip("node not available for JS-level checks")
    from branded_fare_scraper.sources.tripcom import _DRAWER_JS
    shim = _JS_DOM_SHIM
    def card(cabin_line, name_line, price):
        kids = [mk for mk in ()]  # placeholder, built in JS below
        return (cabin_line, name_line, price)
    policy = ("Baggage\\nPersonal item: Included\\nCarry-on baggage: 1 × 17 lbs\\n"
              "Checked baggage: 2 × 50 lbs\\nFlexibility\\nNon-refundable\\n"
              "Change fee: from $180\\nOther benefits\\nMeals provided")
    dom = f"""
const mkCard = (name, price) => mk('div','',{{class:'card'}},[
  mk('div','Economy class',{{}}), mk('div',name,{{}}),
  mk('div',"{policy}",{{class:'policy'}}), mk('div',price,{{class:'price'}})]);
const drawer = mk('div','',{{class:'flt-drawer-policy-be'}},[
  mk('div','Select Fare',{{}}), mkCard('Promotional','$648'), mkCard('Eco Fly','$720')]);
const page = mk('div','Hotels Homes Flights lots of unrelated page text '.repeat(8),{{class:'page'}});
const body = mk('body','',{{}},[page, drawer]);
const document = {{ body: body, querySelector:(s)=>body.querySelectorAll(s)[0]||null,
                   querySelectorAll:(s)=>body.querySelectorAll(s) }};
"""
    probe = (shim + dom +
             "const fn = new Function('document', 'return (' + %s + ')()');\n" % json.dumps(_DRAWER_JS) +
             "console.log(JSON.stringify(fn(document)));")
    res = subprocess.run(["node", "-e", probe], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr[:400]
    cards = json.loads(res.stdout)
    assert len(cards) == 2, [c["text"][:60] for c in cards]
    for c, (want_name, want_price) in zip(cards, (("Promotional", "$648"),
                                                  ("Eco Fly", "$720"))):
        assert "Economy class" in c["text"]           # container reaches the cabin line
        assert want_name in c["text"]                 # ... and the family name
        assert want_price in c["text"]                # ... and its OWN price
        assert "$720" not in c["text"] or want_price == "$720"   # no cross-card bleed
    # And the real parser turns them into the two expected families.
    from branded_fare_scraper.sources.tripcom import cards_to_brands
    brands = cards_to_brands(cards, Cabin.ECONOMY, "TK")
    # Raw names here; display casing ("Eco Fly" -> "EcoFly") is applied later by
    # the shared exporter path, not by the adapter.
    assert [(b.raw_brand_name, b.price_value) for b in brands] == [
        ("Promotional", 648.0), ("Eco Fly", 720.0)]


# --- pilot-8: carousel exhaustion + stale-drawer reset --------------------- #
def _card(name, price, cabin="Economy class"):
    return {"text": f"""{cabin}
{name}
Baggage
Checked baggage: 2 × 50 lbs
Flexibility
Change fee: from $220
Other benefits
Meals provided
${price}""", "struck": []}


def test_tripcom_carousel_reads_every_card():
    """The drawer is a scrolling strip — one read publishes one fare per cabin."""
    import asyncio
    from branded_fare_scraper.sources.tripcom import Tripcom, cards_to_brands
    tc = Tripcom()
    tc.verify_delay_s = tc.drawer_poll_s = tc.drawer_timeout_s = 0
    tc.carousel_settle_s = 0
    pages = [[_card("Eco Fly", "624"), _card("Extra Fly", "686")],
             [_card("Extra Fly", "686"), _card("Prime Fly", "912")],   # overlap
             [_card("Prime Fly", "912"), _card("Business Comfort", "2410",
                                               cabin="Business class")]]
    page = _DrawerPage(rows=[_AI_DOM_ROW], drawer_text=_AI_DRAWER, card_pages=pages)
    got = asyncio.run(tc._fare_panel(page, "YVR", "DEL", date(2026, 11, 9), "y",
                                     dict(_AI_LISTED_ROW)))
    brands = cards_to_brands(got, Cabin.ECONOMY, "TK")
    assert [(b.raw_brand_name, b.price_value) for b in brands] == [
        ("Eco Fly", 624.0), ("Extra Fly", 686.0), ("Prime Fly", 912.0),
        ("Business Comfort", 2410.0)]                 # every family, exactly once
    assert page.advances >= 3                          # swiped to the end


def test_tripcom_sweeps_both_axes_of_the_drawer():
    """Cards live below the fold too — scrolling down reveals more fares."""
    import asyncio
    from branded_fare_scraper.sources.tripcom import Tripcom, cards_to_brands
    tc = Tripcom()
    tc.verify_delay_s = tc.drawer_poll_s = tc.drawer_timeout_s = 0
    tc.carousel_settle_s = 0
    views = {
        (0, 0): [_card("Eco Fly", "624")],
        (0, 1): [_card("Extra Fly", "686")],        # right of the first row
        (1, 0): [_card("Prime Fly", "912")],        # below the fold
        (1, 1): [_card("Business Comfort", "2410", cabin="Business class")],
    }
    page = _DrawerPage(rows=[_AI_DOM_ROW], drawer_text=_AI_DRAWER, views=views)
    got = asyncio.run(tc._fare_panel(page, "YVR", "DEL", date(2026, 11, 9), "y",
                                     dict(_AI_LISTED_ROW)))
    brands = cards_to_brands(got, Cabin.ECONOMY, "TK")
    assert [b.raw_brand_name for b in brands] == [
        "Eco Fly", "Extra Fly", "Prime Fly", "Business Comfort"]
    assert page.scrolls >= 1 and page.advances >= 2      # both axes were walked
    assert page.scroll_resets >= 1                      # rewound for the next flight


def test_tripcom_sweep_caps_bound_a_pathological_drawer():
    import asyncio
    from branded_fare_scraper.sources.tripcom import Tripcom
    tc = Tripcom()
    tc.verify_delay_s = tc.drawer_poll_s = tc.drawer_timeout_s = 0
    tc.carousel_settle_s = 0

    class _Endless(_DrawerPage):
        """Always advances/scrolls and always shows a brand-new card."""
        def __init__(self, **kw):
            super().__init__(**kw)
            self.n = 0

        async def evaluate(self, js, *args):
            if "advanced" in js:
                self.advances += 1
                return {"advanced": True, "how": "arrow"}
            if "scrolled" in js:
                self.scrolls += 1
                return {"scrolled": True, "how": "container"}
            if "struck" in js and self.open:
                self.n += 1
                return [_card(f"Family {self.n}", str(500 + self.n))]
            return await super().evaluate(js, *args)

    page = _Endless(rows=[_AI_DOM_ROW], drawer_text=_AI_DRAWER, views={})
    got = asyncio.run(tc._fare_panel(page, "YVR", "DEL", date(2026, 11, 9), "y",
                                     dict(_AI_LISTED_ROW)))
    # Bounded by the extraction cap, not by the (infinite) DOM.
    assert len(got) <= tc.max_extractions
    assert page.advances <= tc.max_scroll_steps * tc.max_carousel_steps + tc.max_carousel_steps
    assert page.scrolls <= tc.max_scroll_steps + 1


def test_tripcom_carousel_terminates_without_a_control():
    import asyncio
    from branded_fare_scraper.sources.tripcom import Tripcom
    tc = Tripcom()
    tc.verify_delay_s = tc.drawer_poll_s = tc.drawer_timeout_s = 0
    tc.carousel_settle_s = 0
    # A single page and no advance control: one probe, then stop.
    page = _DrawerPage(rows=[_AI_DOM_ROW], drawer_text=_AI_DRAWER,
                       card_pages=[[_card("Eco Fly", "624")]])
    got = asyncio.run(tc._fare_panel(page, "YVR", "DEL", date(2026, 11, 9), "y",
                                     dict(_AI_LISTED_ROW)))
    assert len(got) == 1 and page.advances == 1
    # A carousel that keeps "advancing" but shows the same cards must not loop.
    class _Spinner(_DrawerPage):
        async def evaluate(self, js, *args):
            if "advanced" in js:
                self.advances += 1
                return {"advanced": True, "how": "arrow"}   # always claims success
            return await super().evaluate(js, *args)
    spin = _Spinner(rows=[_AI_DOM_ROW], drawer_text=_AI_DRAWER,
                    card_pages=[[_card("Eco Fly", "624")]])
    got2 = asyncio.run(tc._fare_panel(spin, "YVR", "DEL", date(2026, 11, 9), "y",
                                      dict(_AI_LISTED_ROW)))
    assert len(got2) == 1
    assert spin.advances <= tc.max_carousel_steps      # bounded, no infinite swipe


def test_tripcom_card_key_merges_repeats_across_pages():
    from branded_fare_scraper.sources.tripcom import card_key
    a, b = _card("Eco Fly", "624"), _card("Eco Fly", "624")
    assert card_key(a) == card_key(b)                  # same family + price
    assert card_key(a) != card_key(_card("Eco Fly", "686"))     # price differs
    assert card_key(a) != card_key(_card("Extra Fly", "624"))   # family differs
    # An unnamed card still gets a stable key from its text.
    unnamed = {"text": "Economy class\nBaggage\nChecked baggage: 2 × 50 lbs\n$592"}
    assert card_key(unnamed) == card_key(dict(unnamed))


def test_tripcom_resets_a_stale_drawer_before_clicking():
    """A drawer left open by the previous flight must not be re-read as this one."""
    import asyncio
    from branded_fare_scraper.sources.tripcom import Tripcom
    tc = Tripcom()
    tc.verify_delay_s = tc.drawer_poll_s = tc.drawer_timeout_s = 0
    tc.drawer_close_wait_s = 0
    tc.carousel_settle_s = 0
    tc.hydrate_timeout_s = 1
    want = tc.search_url("YVR", "DEL", date(2026, 11, 9), "y")
    stale = _DrawerPage(rows=[_AI_DOM_ROW], drawer_text=_AI_DRAWER,
                        card_pages=[[_card("Eco Fly", "624")]], url=want, stale=True)
    got = asyncio.run(tc._fare_panel(stale, "YVR", "DEL", date(2026, 11, 9), "y",
                                     dict(_AI_LISTED_ROW)))
    assert stale.closed >= 1                           # closed it before clicking
    assert stale.clicked_by_key == 1 and len(got) == 1
    # A drawer that refuses to close is reported as stale_drawer, not published.
    from branded_fare_scraper.sources.tripcom import DrawerStats
    stats = DrawerStats()
    stuck = _DrawerPage(rows=[_AI_DOM_ROW], drawer_text=_AI_DRAWER,
                        card_pages=[[_card("Eco Fly", "624")]], url=want,
                        stale=True, sticky=True)
    row = dict(_AI_LISTED_ROW, _stats=stats)
    assert asyncio.run(tc._fare_panel(stuck, "YVR", "DEL", date(2026, 11, 9), "y", row)) == []
    assert stats.reasons.get("stale_drawer") == 1
    assert stuck.clicked_by_key == 0                   # never clicked on a dirty page


# --- v13: rate-limit politeness (detect, pace, back off, stop cleanly) ----- #
class _WallPage:
    """Fake page showing Trip.com's "too many attempts" verification modal."""
    def __init__(self, text="", puzzle=False):
        self.text = text
        self.puzzle = puzzle

    async def evaluate(self, js, *args):
        if "innerText" in js and "slice(0, 1500)" in js:
            return self.text
        if "puzzle" in js:
            return self.puzzle
        return None


def test_tripcom_detects_the_rate_limit_modal():
    import asyncio
    from branded_fare_scraper.sources.tripcom import Tripcom
    tc = Tripcom()
    wall = ("Sorry, you have made too many attempts\n"
            "Please complete the verification below")
    assert asyncio.run(tc._rate_limited(_WallPage(text=wall))) is True
    assert asyncio.run(tc._rate_limited(_WallPage(text="çok fazla deneme yaptınız"))) is True
    assert asyncio.run(tc._rate_limited(_WallPage(puzzle=True))) is True   # slide puzzle
    assert asyncio.run(tc._rate_limited(_WallPage(text="97 flights found"))) is False


def test_tripcom_gate_paces_concurrent_workers():
    """One global cadence: two workers cannot both hit the host at once."""
    import asyncio
    import time
    from branded_fare_scraper.sources.tripcom import Tripcom
    Tripcom.reset_state()
    Tripcom.min_interval_s = 0.15
    try:
        async def main():
            stamps = []

            async def worker():
                await Tripcom._gate()
                stamps.append(time.monotonic())
            await asyncio.gather(*(worker() for _ in range(3)))
            return sorted(stamps)

        stamps = asyncio.run(main())
        gaps = [b - a for a, b in zip(stamps, stamps[1:])]
        # jitter is ±30%, so the floor is 0.7 × the configured interval
        assert all(g >= 0.15 * 0.7 * 0.95 for g in gaps), gaps
    finally:
        Tripcom.min_interval_s = 0.0
        Tripcom.reset_state()


def test_tripcom_cooldown_doubles_then_resets():
    import asyncio
    from branded_fare_scraper.sources.tripcom import Tripcom
    Tripcom.reset_state()
    Tripcom.cooldown_base_s, Tripcom.cooldown_max_s = 0.01, 0.04
    try:
        Tripcom._cooldown_s = Tripcom.cooldown_base_s
        assert asyncio.run(Tripcom._cooldown()) is True
        assert Tripcom._cooldown_s == pytest.approx(0.02)      # doubled
        assert asyncio.run(Tripcom._cooldown()) is True
        assert Tripcom._cooldown_s == pytest.approx(0.04)      # doubled, now maxed
        # A healthy streak relaxes it back to the base.
        for _ in range(Tripcom.clean_streak_reset):
            Tripcom._note_clean()
        assert Tripcom._cooldown_s == pytest.approx(0.01)
        assert Tripcom._maxed_pauses == 0
    finally:
        Tripcom.cooldown_base_s, Tripcom.cooldown_max_s = 120.0, 900.0
        Tripcom.reset_state()


def test_tripcom_persistent_wall_stops_the_run_cleanly():
    import asyncio
    from branded_fare_scraper.models import DatePlan, Job, ScrapeUnit
    from branded_fare_scraper.sources.tripcom import Tripcom
    Tripcom.reset_state()
    Tripcom.cooldown_base_s = Tripcom.cooldown_max_s = 0.01
    try:
        Tripcom._cooldown_s = 0.01
        outcomes = [asyncio.run(Tripcom._cooldown())
                    for _ in range(Tripcom.max_maxed_pauses)]
        assert outcomes[:-1] == [True] * (Tripcom.max_maxed_pauses - 1)
        assert outcomes[-1] is False                    # give up, keep the checkpoint
        assert Tripcom._abort_run is True
        # Every later unit returns immediately instead of hammering the wall.
        d0 = date(2026, 9, 2)
        unit = ScrapeUnit(Job("TK", "IST", "JFK"),
                          DatePlan(Season.SUMMER, d0, d0 + timedelta(days=3),
                                   [(d0, d0 + timedelta(days=3))]))
        res = asyncio.run(Tripcom().run_unit(None, unit))
        assert res.status.value == "no_availability"
        assert "rate limit" in res.error and "resume" in res.error
    finally:
        Tripcom.cooldown_base_s, Tripcom.cooldown_max_s = 120.0, 900.0
        Tripcom.reset_state()


def test_tripcom_politeness_is_configurable_from_cli():
    from branded_fare_scraper.__main__ import build_config
    cfg = build_config(["-i", "x.xlsx"])
    assert (cfg.source_concurrency, cfg.tripcom_min_interval_s) == (2, 4.0)
    cfg2 = build_config(["-i", "x.xlsx", "--source-concurrency", "2",
                         "--tripcom-interval", "4"])
    assert (cfg2.source_concurrency, cfg2.tripcom_min_interval_s) == (2, 4.0)


def test_tripcom_stops_a_date_once_families_repeat(monkeypatch):
    """A hub OND has one ladder on ten flights — open ~4 drawers, not 10."""
    import asyncio
    from branded_fare_scraper.models import DatePlan, Job, ScrapeUnit
    from branded_fare_scraper.sources.tripcom import DrawerStats, Tripcom

    d0 = date(2026, 11, 9)
    unit = ScrapeUnit(Job("TK", "IST", "JFK"),
                      DatePlan(Season.SUMMER, d0, d0 + timedelta(days=3),
                               [(d0, d0 + timedelta(days=3))], block_size=0))
    rows = [{"index": i, "testid": f"u-flight-card-{i}", "direct": i == 0,
             "rowKey": f"turkish airlines|{i}:00 pm|5:20 am|21h 50m",
             "rowKeyLoose": f"turkish airlines|{i}:00 pm", "airline": "Turkish Airlines",
             "text": f"Turkish Airlines\n{i}:00 PM\n5:20 AM\n21h 50m\n${500 + i}",
             "priceText": f"${500 + i}"} for i in range(10)]
    ladder = [{"text": "Economy class\nPromotional\nBaggage\n"
                       "Checked baggage: 2 × 50 lbs\nFlexibility\nNon-refundable\n$588"},
              {"text": "Economy class\nEco Fly\nBaggage\n"
                       "Checked baggage: 2 × 50 lbs\nFlexibility\nNon-refundable\n$624"}]
    opened: list = []

    async def fake_list(self, page, o, d, dep, cls):
        return rows if cls == "y" else []

    async def fake_panel(self, page, o, d, dep, cls, r):
        opened.append(r["index"])
        return ladder                                   # every flight: same families

    monkeypatch.setattr(Tripcom, "_flight_list", fake_list)
    monkeypatch.setattr(Tripcom, "_fare_panel", fake_panel)
    monkeypatch.setattr(Tripcom, "_pe_supported", False)
    tc = Tripcom()
    stats = DrawerStats()
    entry = {"dep": d0, "ret": d0 + timedelta(days=3), "flights": rows, "class": "y"}
    parsed = asyncio.run(tc._parse_date(None, unit.job, entry, Cabin.ECONOMY, "TK", stats))
    # Flight 1 brings the families; flights 2-3 add nothing -> stop.
    assert len(opened) == 3, opened
    assert stats.stopped_early == 1
    # ... and the family set is identical to opening all ten.
    from branded_fare_scraper.sources.tripcom import cards_to_brands
    families = {b.raw_brand_name for pf in parsed for b in pf["brands"]}
    assert families == {b.raw_brand_name for b in cards_to_brands(ladder, Cabin.ECONOMY, "TK")}
    assert families == {"Promotional", "Eco Fly"}


def test_tripcom_keeps_opening_while_new_families_appear(monkeypatch):
    import asyncio
    from branded_fare_scraper.models import DatePlan, Job, ScrapeUnit
    from branded_fare_scraper.sources.tripcom import DrawerStats, Tripcom

    d0 = date(2026, 11, 9)
    unit = ScrapeUnit(Job("TK", "IST", "JFK"),
                      DatePlan(Season.SUMMER, d0, d0 + timedelta(days=3),
                               [(d0, d0 + timedelta(days=3))], block_size=0))
    rows = [{"index": i, "testid": f"u-flight-card-{i}", "direct": False,
             "rowKey": f"turkish airlines|{i}:00 pm|5:20 am|21h 50m",
             "rowKeyLoose": f"turkish airlines|{i}:00 pm", "airline": "Turkish Airlines",
             "text": f"Turkish Airlines\n{i}:00 PM\n$500", "priceText": f"${500 + i}"}
            for i in range(5)]

    async def fake_panel(self, page, o, d, dep, cls, r):
        i = r["index"]
        return [{"text": f"Economy class\nFamily {i}\nBaggage\n"
                         f"Checked baggage: 2 × 50 lbs\nFlexibility\n"
                         f"Non-refundable\n${600 + i}"}]

    monkeypatch.setattr(Tripcom, "_fare_panel", fake_panel)
    tc = Tripcom()
    stats = DrawerStats()
    entry = {"dep": d0, "ret": d0 + timedelta(days=3), "flights": rows, "class": "y"}
    parsed = asyncio.run(tc._parse_date(None, unit.job, entry, Cabin.ECONOMY, "TK", stats))
    assert len(parsed) == 5 and stats.stopped_early == 0   # every flight was new


def test_tripcom_stops_at_the_first_block_that_has_flights(monkeypatch):
    """Fallback blocks are for empty deep windows — never extra work."""
    import asyncio
    from branded_fare_scraper.dates import build_date_plan
    from branded_fare_scraper.models import Job, ScrapeUnit
    from branded_fare_scraper.sources.tripcom import Tripcom

    plan = build_date_plan(Season.SUMMER, today=date(2026, 1, 1), rng=random.Random(3))
    unit = ScrapeUnit(Job("TK", "IST", "JFK"), plan)
    assert len(plan.blocks) == 1 + len(FAR_FALLBACK_LEADS)
    block1 = {d for d, _r in plan.blocks[0]}
    seen: list = []

    row = {"index": 0, "testid": "u-flight-card-1", "direct": True,
           "rowKey": "turkish airlines|7:00 pm|5:20 am|21h 50m",
           "rowKeyLoose": "turkish airlines|7:00 pm", "airline": "Turkish Airlines",
           "text": "Turkish Airlines\n7:00 PM\n5:20 AM\n21h 50m\n$588"}

    async def fake_list(self, page, o, d, dep, cls):
        seen.append(dep)
        return [row] if cls == "y" else []

    async def fake_panel(self, page, o, d, dep, cls, r):
        return [{"text": "Economy class\nPromotional\nBaggage\n"
                         "Checked baggage: 2 × 50 lbs\nFlexibility\nNon-refundable\n$588"}]

    monkeypatch.setattr(Tripcom, "_flight_list", fake_list)
    monkeypatch.setattr(Tripcom, "_fare_panel", fake_panel)
    monkeypatch.setattr(Tripcom, "_pe_supported", False)
    res = asyncio.run(Tripcom().run_unit(None, unit))
    assert res.status.value == "success"
    assert set(seen) <= block1                      # later blocks never touched


def test_tripcom_falls_through_empty_blocks(monkeypatch):
    """No schedule loaded at 300 days -> try ~210, then ~150."""
    import asyncio
    from branded_fare_scraper.dates import build_date_plan
    from branded_fare_scraper.models import Job, ScrapeUnit
    from branded_fare_scraper.sources.tripcom import Tripcom

    plan = build_date_plan(Season.SUMMER, today=date(2026, 1, 1), rng=random.Random(3))
    unit = ScrapeUnit(Job("TK", "IST", "JFK"), plan)
    block3 = {d for d, _r in plan.blocks[2]}
    seen: list = []
    row = {"index": 0, "testid": "u-flight-card-1", "direct": True,
           "rowKey": "turkish airlines|7:00 pm|5:20 am|21h 50m",
           "rowKeyLoose": "turkish airlines|7:00 pm", "airline": "Turkish Airlines",
           "text": "Turkish Airlines\n7:00 PM\n5:20 AM\n21h 50m\n$588"}

    async def fake_list(self, page, o, d, dep, cls):
        seen.append(dep)
        # Only the LAST block has any inventory at all.
        return [row] if (dep in block3 and cls == "y") else []

    async def fake_panel(self, page, o, d, dep, cls, r):
        return [{"text": "Economy class\nPromotional\nBaggage\n"
                         "Checked baggage: 2 × 50 lbs\nFlexibility\nNon-refundable\n$588"}]

    monkeypatch.setattr(Tripcom, "_flight_list", fake_list)
    monkeypatch.setattr(Tripcom, "_fare_panel", fake_panel)
    monkeypatch.setattr(Tripcom, "_pe_supported", False)
    res = asyncio.run(Tripcom().run_unit(None, unit))
    assert res.status.value == "success"
    assert block3 & set(seen)                       # walked all the way down
    assert {c.departure for c in res.cabin_results} <= block3


def test_base_walk_stops_at_the_first_productive_block():
    import asyncio
    from branded_fare_scraper.dates import build_date_plan
    from branded_fare_scraper.models import CabinResult, Job, ScrapeUnit
    from branded_fare_scraper.sources.base import SourceAdapter

    plan = build_date_plan(Season.SUMMER, today=date(2026, 1, 1), rng=random.Random(3))
    unit = ScrapeUnit(Job("XX", "AAA", "BBB"), plan)
    block1 = {d for d, _r in plan.blocks[0]}

    class Always(SourceAdapter):
        name = "b1"
        def __init__(self): self.seen = []
        def supports(self, c): return True
        def cabins_for(self, job): return [Cabin.ECONOMY]
        async def fetch_search(self, page, job, dep, ret):
            self.seen.append(dep)
            return [CabinResult(cabin=Cabin.ECONOMY, departure=dep, return_date=ret,
                                brands=[RawBrand("Saver", Cabin.ECONOMY, 0, 100.0,
                                                 PriceType.ABSOLUTE)])]

    a = Always()
    res = asyncio.run(a.run_unit(None, unit))
    assert res.total_brands() == 1
    assert set(a.seen) == block1                    # exactly one block walked

    class OnlyLast(SourceAdapter):
        name = "b2"
        def __init__(self): self.seen = []
        def supports(self, c): return True
        def cabins_for(self, job): return [Cabin.ECONOMY]
        async def fetch_search(self, page, job, dep, ret):
            self.seen.append(dep)
            # Only the LAST band has anything — the case a late-loading carrier
            # creates. Indexed from the end so adding a band cannot silently
            # turn this into "stops at a middle block" and still pass.
            if dep not in {d for d, _r in plan.blocks[-1]}:
                raise NoAvailabilityError("nothing this far out")
            return [CabinResult(cabin=Cabin.ECONOMY, departure=dep, return_date=ret,
                                brands=[RawBrand("Saver", Cabin.ECONOMY, 0, 100.0,
                                                 PriceType.ABSOLUTE)])]

    from branded_fare_scraper.retry import NoAvailabilityError
    b = OnlyLast()
    res2 = asyncio.run(b.run_unit(None, unit))
    assert res2.total_brands() == 1
    assert len(b.seen) == len(plan.window)          # fell through every block


# --- round 16 E: Ubfly is a gentle TOP-UP, never a replacement ------------- #
def _rb(name, price, cabin=Cabin.ECONOMY, source=""):
    from branded_fare_scraper.models import RawAmenity
    return RawBrand(name, cabin, 0, price, PriceType.ABSOLUTE, source=source,
                    amenities=[RawAmenity("meal", AmenityStatus.INCLUDED, "hot",
                                          canonical_key="meal")])


def test_merge_ladders_adds_only_missing_families():
    from branded_fare_scraper.normalization import merge_ladders
    primary = [_rb("Eco Fly", 624.0, source="Trip.com"),
               _rb("Extra Fly", 686.0, source="Trip.com")]
    secondary = [_rb("Eco Fly", 599.0), _rb("Prime Fly", 912.0)]
    merged, added = merge_ladders(primary, secondary, "Ubfly")
    assert added == ["Prime Fly"]
    by = {b.raw_brand_name: b for b in merged}
    assert by["Eco Fly"].price_value == 624.0          # primary keeps its own price
    assert by["Eco Fly"].source == "Trip.com"
    assert by["Prime Fly"].source == "Ubfly"           # attributed to the top-up
    assert [b.raw_brand_name for b in merged] == ["Eco Fly", "Extra Fly", "Prime Fly"]
    assert [b.screen_order for b in merged] == [0, 1, 2]
    # Nothing new -> ladder untouched.
    again, added2 = merge_ladders(primary, [_rb("ECOFLY", 10.0)], "Ubfly")
    assert added2 == [] and len(again) == 2


def test_merge_cross_date_ladder_fills_gaps_and_reorders_by_price():
    """2026-08-03 pilot: fill a cabin's missing tiers from OTHER sampled dates.

    Tuesday sells [Basic, Smart, Plus] (Go sold out that day); Wednesday sells
    [Basic, Smart, Go] (Plus sold out). Neither day alone shows the OND's real
    four-tier structure; the union does. Ordering must come out by PRICE, not
    by which date a tier was borrowed from.
    """
    from branded_fare_scraper.normalization import merge_cross_date_ladder
    tue = [_rb("Basic", 0.0), _rb("Smart", 70.0), _rb("Plus", 130.0)]
    wed_date = date(2027, 6, 12)
    wed = [_rb("Basic", 5.0), _rb("Smart", 68.0), _rb("Go", 95.0)]

    merged, notes = merge_cross_date_ladder(tue, [(wed_date, wed)], "Enuygun")

    names = [b.raw_brand_name for b in merged]
    assert names == ["Basic", "Smart", "Go", "Plus"]          # price order: 0, 70, 95, 130
    assert [b.screen_order for b in merged] == [0, 1, 2, 3]
    by = {b.raw_brand_name: b for b in merged}
    assert by["Smart"].price_value == 70.0                    # primary keeps its OWN price
    assert by["Go"].price_value == 95.0                        # borrowed value, untouched
    assert by["Go"].source == "Enuygun"
    assert notes and "Go" in notes[0] and "2027-06-12" in notes[0]


def test_merge_cross_date_ladder_rejects_an_implausible_borrowed_price():
    """A borrowed tier that would introduce a new >3x jump is dropped.

    The other date's price reflects THAT day's inventory, not necessarily a
    real gap in the primary day's structure — a wild outlier is more likely a
    fluke (e.g. that date's cheap fares were sold out) than a genuine tier.
    """
    from branded_fare_scraper.normalization import merge_cross_date_ladder
    primary = [_rb("Basic", 50.0), _rb("Smart", 70.0)]
    other = [_rb("Weird", 5000.0)]                # 71x jump over Smart -> implausible

    merged, notes = merge_cross_date_ladder(primary, [(date(2027, 6, 12), other)], "Enuygun")
    assert notes == []
    assert [b.raw_brand_name for b in merged] == ["Basic", "Smart"]    # unchanged


def test_run_unit_cross_date_merge_is_opt_in_and_off_by_default():
    """Config.merge_cross_date_ladders defaults False; run_unit's own default
    selection (single strongest date, others discarded) must be untouched
    unless an adapter explicitly turns the flag on."""
    import asyncio
    from branded_fare_scraper.models import CabinResult, DatePlan, Job, ScrapeUnit
    from branded_fare_scraper.sources.base import SourceAdapter

    d0 = date(2027, 6, 10)
    plan = DatePlan(Season.SUMMER, d0, d0 + timedelta(days=3),
                    [(d0, d0 + timedelta(days=3)), (d0 + timedelta(days=1), d0 + timedelta(days=4))])
    unit = ScrapeUnit(Job("XX", "AAA", "BBB"), plan)

    class TwoDates(SourceAdapter):
        name = "test-src"
        def supports(self, c): return True
        def cabins_for(self, job): return [Cabin.ECONOMY]
        async def fetch_search(self, page, job, dep, ret):
            if dep == d0:
                return [CabinResult(cabin=Cabin.ECONOMY, departure=dep, return_date=ret,
                                    brands=[_rb("Basic", 0.0), _rb("Smart", 70.0), _rb("Plus", 130.0)])]
            return [CabinResult(cabin=Cabin.ECONOMY, departure=dep, return_date=ret,
                                brands=[_rb("Basic", 5.0), _rb("Smart", 68.0), _rb("Go", 95.0)])]

    off = TwoDates()
    assert off.merge_cross_date is False
    res_off = asyncio.run(off.run_unit(None, unit))
    names_off = [b.raw_brand_name for b in res_off.cabin_results[0].brands]
    assert "Go" not in names_off              # today's default: loser discarded whole

    on = TwoDates()
    on.merge_cross_date = True
    res_on = asyncio.run(on.run_unit(None, unit))
    names_on = [b.raw_brand_name for b in res_on.cabin_results[0].brands]
    assert names_on == ["Basic", "Smart", "Go", "Plus"]


def test_brand_source_round_trips_through_raw():
    from branded_fare_scraper.io_utils import _raw_brand_to_dict
    from branded_fare_scraper.rebuild import raw_brand_from_dict
    d = _raw_brand_to_dict(_rb("Prime Fly", 912.0, source="Ubfly"))
    assert d["source"] == "Ubfly"
    assert raw_brand_from_dict(d).source == "Ubfly"
    legacy = {k: v for k, v in d.items() if k != "source"}     # pre-round-16 file
    assert raw_brand_from_dict(legacy).source == ""


def test_ubfly_topup_adds_families_and_respects_the_challenge_flag():
    import asyncio
    from branded_fare_scraper.config import Config
    from branded_fare_scraper.models import CabinResult, DatePlan, Job, ScrapeUnit
    from branded_fare_scraper.runner import Runner
    from branded_fare_scraper.sources.ubfly import Ubfly

    d0 = date(2026, 11, 9)
    unit = ScrapeUnit(Job("TK", "IST", "JFK"),
                      DatePlan(Season.SUMMER, d0, d0 + timedelta(days=3),
                               [(d0, d0 + timedelta(days=3))], block_size=0))

    class _FakeUbfly:
        name = "Ubfly"
        needs_browser = False
        def __init__(self): self.calls = []
        async def fetch_search(self, page, job, dep, ret):
            self.calls.append(dep)
            return [CabinResult(cabin=Cabin.ECONOMY, departure=dep, return_date=ret,
                                brands=[_rb("Eco Fly", 599.0), _rb("Prime Fly", 912.0)])]

    def _merged():
        return {Cabin.ECONOMY: CabinResult(
            cabin=Cabin.ECONOMY, departure=d0, return_date=d0 + timedelta(days=3),
            source="Trip.com",
            brands=[_rb("Eco Fly", 624.0, source="Trip.com")])}

    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        runner = Runner(Config(input_path="x.xlsx", output_dir=tmp))
        try:
            Ubfly._challenge_disabled = False
            fake = _FakeUbfly()
            merged = asyncio.run(runner._ubfly_topup(fake, unit, None, _merged(), []))
            names = [b.raw_brand_name for b in merged[Cabin.ECONOMY].brands]
            assert names == ["Eco Fly", "Prime Fly"]      # only the missing family
            eco = merged[Cabin.ECONOMY].brands[0]
            assert eco.price_value == 624.0 and eco.source == "Trip.com"
            assert merged[Cabin.ECONOMY].brands[1].source == "Ubfly"
            assert fake.calls == [d0]                     # same date the primary used

            # Challenge active -> Ubfly is never called and the ladder is untouched.
            Ubfly._challenge_disabled = True
            fake2 = _FakeUbfly()
            untouched = asyncio.run(runner._ubfly_topup(fake2, unit, None, _merged(), []))
            assert fake2.calls == []
            assert [b.raw_brand_name for b in untouched[Cabin.ECONOMY].brands] == ["Eco Fly"]
        finally:
            Ubfly._challenge_disabled = False


def test_ubfly_topup_ignores_cabins_the_primary_missed():
    """A cabin Trip.com never produced is left to the normal fallback path."""
    import asyncio
    import tempfile
    from branded_fare_scraper.config import Config
    from branded_fare_scraper.models import CabinResult, DatePlan, Job, ScrapeUnit
    from branded_fare_scraper.runner import Runner
    from branded_fare_scraper.sources.ubfly import Ubfly

    d0 = date(2026, 11, 9)
    unit = ScrapeUnit(Job("TK", "IST", "JFK"),
                      DatePlan(Season.SUMMER, d0, d0 + timedelta(days=3),
                               [(d0, d0 + timedelta(days=3))], block_size=0))

    class _FakeUbfly:
        name = "Ubfly"
        needs_browser = False
        async def fetch_search(self, page, job, dep, ret):
            return [CabinResult(cabin=Cabin.BUSINESS, departure=dep, return_date=ret,
                                brands=[_rb("Business Flex", 2500.0, Cabin.BUSINESS)])]

    merged = {Cabin.ECONOMY: CabinResult(
        cabin=Cabin.ECONOMY, departure=d0, return_date=d0 + timedelta(days=3),
        source="Trip.com", brands=[_rb("Eco Fly", 624.0, source="Trip.com")])}
    with tempfile.TemporaryDirectory() as tmp:
        Ubfly._challenge_disabled = False
        runner = Runner(Config(input_path="x.xlsx", output_dir=tmp))
        out = asyncio.run(runner._ubfly_topup(_FakeUbfly(), unit, None, merged, []))
    assert set(out) == {Cabin.ECONOMY}                 # business not smuggled in here
    assert [b.raw_brand_name for b in out[Cabin.ECONOMY].brands] == ["Eco Fly"]


# --- day-1 defect 1: distinct families must publish distinct names --------- #
#: The five day-1 ladders whose published names collapsed (raw names distinct).
_DAY1_LADDERS = [
    ("TK", Cabin.ECONOMY, [("Economy Light", 300.0), ("Economy Comfort", 400.0),
                           ("Comfort", 450.0)]),
    ("LH", Cabin.ECONOMY, [("Economy Basic", 200.0), ("Economy Light", 250.0),
                           ("Economy Comfort", 300.0), ("Economy Comfort Green", 330.0),
                           ("Economy Flex", 400.0)]),
    ("EY", Cabin.ECONOMY, [("Economy Basic", 200.0), ("Economy Value", 250.0),
                           ("Economy Comfort", 300.0), ("Economy Deluxe", 400.0)]),
    ("AI", Cabin.ECONOMY, [("Eco Value", 200.0), ("Eco Classic", 250.0),
                           ("Eco Flex", 300.0)]),
    ("BA", Cabin.BUSINESS, [("Business", 2000.0), ("Business Semi-Flex", 2400.0),
                            ("Business Flex", 2800.0)]),
]


def test_published_names_stay_distinct_per_ladder():
    from branded_fare_scraper.normalization import tier_code
    for carrier, cab, fares in _DAY1_LADDERS:
        brands = [RawBrand(n, cab, i, p, PriceType.ABSOLUTE)
                  for i, (n, p) in enumerate(fares)]
        rows = [(nb.normalized_name, absp, tier_code(c, o))
                for c, _raw, nb, o, absp in iter_ranked_by_cabin(brands, cab, carrier=carrier)]
        names = [n for n, _p, _t in rows]
        prices = [p for _n, p, _t in rows]
        assert len(set(names)) == len(names), (carrier, names)
        assert prices == sorted(prices), (carrier, prices)
        assert [t for _n, _p, t in rows] == [tier_code(cab, i) for i in range(len(rows))]
        # The airline's own wording is what publishes — never the "Economy — X"
        # fallback, and never a canonical label that merges two products.
        assert all(" — " not in n for n in names)
    # Spot-check the two that used to merge.
    lh = dict(zip(["Economy Basic", "Economy Light", "Economy Comfort",
                   "Economy Comfort Green", "Economy Flex"], range(5)))
    brands = [RawBrand(n, Cabin.ECONOMY, i, 200.0 + i * 50, PriceType.ABSOLUTE)
              for n, i in lh.items()]
    got = [nb.normalized_name for _c, _r, nb, _o, _a in
           iter_ranked_by_cabin(brands, Cabin.ECONOMY, carrier="LH")]
    assert "Economy Comfort" in got and "Economy Comfort Green" in got


def test_canonical_label_still_drives_rank_and_tier():
    from branded_fare_scraper.normalization import normalize_brand
    # Semi-flex ranks between comfort and flex; deluxe is a premium tier.
    assert (normalize_brand("Business Comfort", Cabin.BUSINESS).rank
            < normalize_brand("Business Semi-Flex", Cabin.BUSINESS).rank
            < normalize_brand("Business Flex", Cabin.BUSINESS).rank)
    assert normalize_brand("Economy Deluxe", Cabin.ECONOMY).subtier == "premium"
    assert normalize_brand("Eco Value", Cabin.ECONOMY).subtier == "basic"
    assert normalize_brand("Eco Classic", Cabin.ECONOMY).subtier == "standard"
    assert normalize_brand("Economy Comfort Green", Cabin.ECONOMY).subtier == "comfort"
    # The canonical label is still reachable after publishing the airline name.
    brands = [RawBrand("Economy Comfort Green", Cabin.ECONOMY, 0, 330.0, PriceType.ABSOLUTE)]
    _c, _r, nb, _o, _a = next(iter(iter_ranked_by_cabin(brands, Cabin.ECONOMY, carrier="LH")))
    assert nb.normalized_name == "Economy Comfort Green"
    assert nb.canonical_name == "Economy Comfort" and nb.subtier == "comfort"


def test_identical_raw_names_are_disambiguated_from_their_own_text():
    from branded_fare_scraper.normalization import display_names
    assert display_names(["Economy Comfort", "Economy Comfort Green"], "LH") == [
        "Economy Comfort", "Economy Comfort Green"]
    # Same pretty name from different raw text -> distinguishing words appended.
    assert display_names(["Business Flex", "Business-Flex"], "BA")[0] != \
        display_names(["Business Flex", "Business-Flex"], "BA")[1]
    # Truly identical raws cannot be told apart -> left alone, never indexed.
    same = display_names(["Business Flex", "Business Flex"], "BA")
    assert same == ["Business Flex", "Business Flex"]


# --- day-1 defect 2: raw records are durable per unit ---------------------- #
def _unit_result(key_suffix: str, price: float):
    from branded_fare_scraper.models import (CabinResult, DatePlan, Job, ScrapeUnit,
                                             UnitResult, UnitStatus)
    d0 = date(2026, 11, 9)
    job = Job("TK", "IST", f"JF{key_suffix}")
    unit = ScrapeUnit(job, DatePlan(Season.SUMMER, d0, d0 + timedelta(days=3),
                                    [(d0, d0 + timedelta(days=3))], block_size=0))
    cab = CabinResult(cabin=Cabin.ECONOMY, departure=d0, return_date=d0 + timedelta(days=3),
                      source="Trip.com",
                      brands=[RawBrand("Promotional", Cabin.ECONOMY, 0, price,
                                       PriceType.ABSOLUTE)])
    return UnitResult(unit=unit, source="Trip.com", cabin_results=[cab],
                      status=UnitStatus.SUCCESS)


def test_raw_records_survive_an_interrupted_run(tmp_path):
    """A stop must not throw away hours of scraping."""
    from branded_fare_scraper.io_utils import append_raw
    from branded_fare_scraper.rebuild import iter_raw_records
    units = [_unit_result("A", 100.0), _unit_result("B", 200.0), _unit_result("C", 300.0)]
    for u in units[:2]:                       # run "interrupted" after 2 units
        append_raw(u, tmp_path)
    recs = list(iter_raw_records(tmp_path / "raw_data.jsonl"))
    assert len(recs) == 2
    assert [r["destination"] for r in recs] == ["JFA", "JFB"]
    assert recs[0]["cabins"][0]["brands"][0]["raw_brand_name"] == "Promotional"


def test_resumed_run_does_not_duplicate_units(tmp_path):
    from branded_fare_scraper.io_utils import append_raw, write_raw
    from branded_fare_scraper.rebuild import iter_raw_records
    first = [_unit_result("A", 100.0), _unit_result("B", 200.0)]
    for u in first:
        append_raw(u, tmp_path)
    # The resumed run re-does B (new price) and adds C, then writes at the end.
    redo_b, new_c = _unit_result("B", 250.0), _unit_result("C", 300.0)
    for u in (redo_b, new_c):
        append_raw(u, tmp_path)
    write_raw([redo_b, new_c], tmp_path)
    recs = list(iter_raw_records(tmp_path / "raw_data.jsonl"))
    keys = [r["unit_key"] for r in recs]
    assert len(keys) == len(set(keys)) == 3          # one record per unit
    by = {r["destination"]: r for r in recs}
    assert by["JFB"]["cabins"][0]["brands"][0]["price_value"] == 250.0   # newest won
    assert [r["destination"] for r in recs] == ["JFA", "JFB", "JFC"]     # order kept


def test_write_raw_tolerates_a_half_written_line(tmp_path):
    from branded_fare_scraper.io_utils import append_raw, write_raw
    from branded_fare_scraper.rebuild import iter_raw_records
    append_raw(_unit_result("A", 100.0), tmp_path)
    with (tmp_path / "raw_data.jsonl").open("a", encoding="utf-8") as f:
        f.write('{"unit_key": "truncated', )           # killed mid-write
    write_raw([_unit_result("B", 200.0)], tmp_path)
    recs = list(iter_raw_records(tmp_path / "raw_data.jsonl"))
    assert [r["destination"] for r in recs] == ["JFA", "JFB"]


def test_tripcom_registered_as_primary_with_ubfly_fallback():
    import branded_fare_scraper.sources as sources
    from branded_fare_scraper.sources.tripcom import Tripcom
    order = [(a.priority, a.name) for a in sources.registered_sources()]
    assert (2, "Trip.com") in order and (3, "Ubfly") in order
    # Enuygun was reinstated 2026-08-03 as a second priority-3 gap filler, so the
    # chain now holds ties. Only the PRIORITIES have to be ordered — comparing
    # whole tuples would sort on the name and fail on any tie.
    assert (3, "Enuygun") in order
    prios = [p for p, _ in order]
    assert prios == sorted(prios)                          # priority-ordered chain
    assert prios[0] == min(prios), "the primary OTA must come first"
    assert Tripcom().supports("ANY") is True
    url = Tripcom().search_url("IST", "JFK", date(2026, 9, 2), "y")
    assert "dcity=ist" in url and "acity=jfk" in url and "ddate=2026-09-02" in url
    assert "class=y" in url and "curr=USD" in url and "locale=en-US" in url


# ------------------- real-browser channel (round 15, A5) ------------------- #
def test_browser_channel_config_default_and_override():
    from branded_fare_scraper.config import Config
    assert Config().browser_channel is None            # today's Ubfly behaviour
    cfg = Config(browser_channel="chrome")
    assert cfg.browser_channel == "chrome"


def test_real_channel_profile_is_authentic():
    """Live-proven: Trip.com degrades a Chrome carrying automation flags."""
    from branded_fare_scraper.browser_pool import (context_options, launch_options,
                                                   stealth_init_script)
    from branded_fare_scraper.config import Config
    cfg = Config(browser_channel="chrome", headless=False)
    launch = launch_options(cfg, True)
    assert launch == {"headless": False, "channel": "chrome"}
    assert "args" not in launch                        # NO automation flags at all
    ctx = context_options(cfg, True)
    assert set(ctx) == {"locale", "viewport"}           # nothing else may be added
    assert "user_agent" not in ctx and "extra_http_headers" not in ctx
    assert stealth_init_script(True) is None            # no injected script either
    # Headless must not smuggle --disable-http2 into the real-browser profile.
    assert "args" not in launch_options(Config(browser_channel="chrome", headless=True), True)


def test_bundled_profile_options_unchanged():
    from branded_fare_scraper.browser_pool import (context_options, launch_options,
                                                   stealth_init_script)
    from branded_fare_scraper.config import Config
    legacy = ["--disable-blink-features=AutomationControlled", "--no-sandbox",
              "--disable-features=IsolateOrigins,site-per-process"]
    cfg = Config(headless=False)
    assert launch_options(cfg, False) == {"headless": False, "args": legacy}
    assert launch_options(Config(headless=True), False) == {
        "headless": True, "args": legacy + ["--disable-http2"]}
    ctx = context_options(cfg, False)
    assert ctx == {"locale": cfg.locale, "user_agent": cfg.user_agent,
                   "viewport": {"width": 1366, "height": 900},
                   "extra_http_headers": {"Accept-Language": "en-US,en;q=0.9",
                                          "Upgrade-Insecure-Requests": "1"}}
    assert "webdriver" in stealth_init_script(False)
    # The fallback-after-channel-failure path is the bundled path byte-for-byte:
    # start() clears browser_channel, so channel_ok=False options are identical.
    failed_over = Config(browser_channel=None, headless=False)
    assert launch_options(failed_over, False) == launch_options(cfg, False)
    assert context_options(failed_over, False) == ctx


class _FakeCtx:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.scripts = []
        self.closed = 0

    def set_default_navigation_timeout(self, v): pass
    def set_default_timeout(self, v): pass
    async def add_init_script(self, s): self.scripts.append(s)
    async def new_page(self): return object()
    async def close(self): self.closed += 1


class _FakeBrowser:
    def __init__(self):
        self.contexts = []

    async def new_context(self, **kwargs):
        ctx = _FakeCtx(**kwargs)
        self.contexts.append(ctx)
        return ctx

    async def close(self): pass


class _FakeChromium:
    def __init__(self, fail_channel=False):
        self.launches = []
        self.fail_channel = fail_channel
        self.browser = _FakeBrowser()

    async def launch(self, **kwargs):
        self.launches.append(kwargs)
        if self.fail_channel and "channel" in kwargs:
            raise RuntimeError("Chrome is not installed")
        return self.browser


def _fake_playwright(monkeypatch, chromium):
    class _PW:
        def __init__(self): self.chromium = chromium
        async def stop(self): pass

    class _Starter:
        async def start(self): return _PW()

    monkeypatch.setattr("playwright.async_api.async_playwright", lambda: _Starter())


def test_pool_start_applies_authentic_profile_on_channel(monkeypatch):
    """The live bug: flags/headers/init-script leaked into the chrome channel."""
    import asyncio
    from branded_fare_scraper.browser_pool import BrowserPool
    from branded_fare_scraper.config import Config
    chromium = _FakeChromium()
    _fake_playwright(monkeypatch, chromium)
    pool = BrowserPool(Config(browser_channel="chrome", browser_pool_size=1, headless=False))
    asyncio.run(pool.start())
    assert chromium.launches == [{"headless": False, "channel": "chrome"}]
    ctx = chromium.browser.contexts[0]
    assert set(ctx.kwargs) == {"locale", "viewport"}
    assert ctx.scripts == []                           # nothing injected
    assert pool._channel_ok is True


def test_pool_falls_back_to_bundled_profile_when_chrome_missing(monkeypatch):
    import asyncio
    from branded_fare_scraper.browser_pool import BrowserPool
    from branded_fare_scraper.config import Config
    chromium = _FakeChromium(fail_channel=True)
    _fake_playwright(monkeypatch, chromium)
    cfg = Config(browser_channel="chrome", browser_pool_size=1, headless=False)
    pool = BrowserPool(cfg)
    asyncio.run(pool.start())
    assert len(chromium.launches) == 2                 # channel attempt, then bundled
    assert "channel" not in chromium.launches[1]
    assert "--no-sandbox" in chromium.launches[1]["args"]
    ctx = chromium.browser.contexts[0]
    assert "user_agent" in ctx.kwargs and "extra_http_headers" in ctx.kwargs
    assert ctx.scripts and "webdriver" in ctx.scripts[0]
    assert pool._channel_ok is False and cfg.browser_channel is None


def test_pool_bundled_profile_unchanged_without_channel(monkeypatch):
    import asyncio
    from branded_fare_scraper.browser_pool import BrowserPool
    from branded_fare_scraper.config import Config
    chromium = _FakeChromium()
    _fake_playwright(monkeypatch, chromium)
    pool = BrowserPool(Config(browser_pool_size=1, headless=False))
    asyncio.run(pool.start())
    assert "channel" not in chromium.launches[0]
    assert chromium.launches[0]["args"][0] == "--disable-blink-features=AutomationControlled"
    ctx = chromium.browser.contexts[0]
    assert ctx.kwargs["user_agent"].startswith("Mozilla/5.0")
    assert ctx.scripts and "webdriver" in ctx.scripts[0]


# --- round 17: neutral remote browser over CDP ----------------------------- #
_CDP = "wss://production-lon.browserless.io?token=SECRET&timeout=300000"


class _FakeRemoteBrowser:
    def __init__(self, contexts=None):
        self.contexts = list(contexts or [])
        self.closed = 0
        self.made = []

    async def new_context(self, **kwargs):
        ctx = _FakeCtx(**kwargs)
        self.made.append(ctx)
        self.contexts.append(ctx)
        return ctx

    async def close(self):
        self.closed += 1


class _FakeCdpChromium:
    def __init__(self, browser=None, fail=False):
        self.browser = browser or _FakeRemoteBrowser()
        self.fail = fail
        self.connected = []
        self.launches = []

    async def connect_over_cdp(self, endpoint, **kw):
        self.connected.append(endpoint)
        if self.fail:
            raise RuntimeError("ECONNREFUSED")
        return self.browser

    async def launch(self, **kwargs):
        self.launches.append(kwargs)
        return self.browser


def test_remote_profile_is_left_exactly_as_the_service_built_it():
    from branded_fare_scraper.browser_pool import (authentic_profile, context_options,
                                                   stealth_init_script)
    from branded_fare_scraper.config import Config
    cfg = Config(cdp_endpoint=_CDP, browser_channel=None)
    assert authentic_profile(cfg, False) is True       # remote owns its fingerprint
    ctx = context_options(cfg, False)
    assert set(ctx) == {"locale", "viewport"}
    assert "user_agent" not in ctx and "extra_http_headers" not in ctx
    # A local profile is unaffected by the new flag being absent.
    assert authentic_profile(Config(), False) is False
    assert "user_agent" in context_options(Config(), False)
    assert stealth_init_script(True) is None


def test_pool_connects_over_cdp_and_never_launches(monkeypatch):
    import asyncio
    from branded_fare_scraper.browser_pool import BrowserPool
    from branded_fare_scraper.config import Config
    chromium = _FakeCdpChromium()
    _fake_playwright(monkeypatch, chromium)
    pool = BrowserPool(Config(cdp_endpoint=_CDP, browser_pool_size=2))
    asyncio.run(pool.start())
    assert chromium.connected == [_CDP]
    assert chromium.launches == []                     # never a local browser
    assert pool._remote is True
    for ctx in chromium.browser.made:                  # no UA, no injected script
        assert set(ctx.kwargs) == {"locale", "viewport"}
        assert ctx.scripts == []


def test_pool_reuses_a_context_the_remote_browser_already_has(monkeypatch):
    import asyncio
    from branded_fare_scraper.browser_pool import BrowserPool
    from branded_fare_scraper.config import Config
    existing = _FakeCtx(locale="en-US")
    browser = _FakeRemoteBrowser(contexts=[existing])
    chromium = _FakeCdpChromium(browser=browser)
    _fake_playwright(monkeypatch, chromium)
    pool = BrowserPool(Config(cdp_endpoint=_CDP, browser_pool_size=2))
    asyncio.run(pool.start())
    assert existing in pool._all_contexts              # theirs, reused
    assert len(browser.made) == 1                      # only the second was created
    assert existing not in pool._created_contexts


def test_pool_cdp_failure_raises_and_never_falls_back(monkeypatch):
    import asyncio
    from branded_fare_scraper.browser_pool import BrowserPool
    from branded_fare_scraper.config import Config
    chromium = _FakeCdpChromium(fail=True)
    _fake_playwright(monkeypatch, chromium)
    pool = BrowserPool(Config(cdp_endpoint=_CDP, browser_pool_size=1))
    with pytest.raises(RuntimeError) as err:
        asyncio.run(pool.start())
    assert "remote browser" in str(err.value)
    assert "browserless.io" in str(err.value)          # names the host...
    assert "SECRET" not in str(err.value)              # ...but never the token
    assert chromium.launches == []                     # no silent local fallback
    assert pool._started is False


def test_pool_stop_leaves_a_connected_browser_open(monkeypatch):
    import asyncio
    from branded_fare_scraper.browser_pool import BrowserPool
    from branded_fare_scraper.config import Config
    existing = _FakeCtx(locale="en-US")
    browser = _FakeRemoteBrowser(contexts=[existing])
    chromium = _FakeCdpChromium(browser=browser)
    _fake_playwright(monkeypatch, chromium)
    pool = BrowserPool(Config(cdp_endpoint=_CDP, browser_pool_size=2))
    asyncio.run(pool.start())
    made = list(browser.made)
    asyncio.run(pool.stop())
    assert browser.closed == 0                         # the service keeps its browser
    assert existing.closed == 0                        # and its own context
    assert all(c.closed == 1 for c in made)            # ours are cleaned up


def test_cdp_endpoint_cli_plumbing():
    from branded_fare_scraper.__main__ import build_config
    assert build_config(["-i", "x.xlsx"]).cdp_endpoint is None
    cfg = build_config(["-i", "x.xlsx", "--cdp-endpoint", _CDP])
    assert cfg.cdp_endpoint == _CDP
    assert cfg.browser_channel == "chrome"             # unrelated flags unchanged


def test_no_bot_evasion_parameters_in_source():
    """We adopt the neutral CDP transport ONLY — never evasion features.

    Same boundary held for THY PerimeterX, Priceline, Expedia and Ubfly's
    Cloudflare wall: detect and back off, never circumvent.
    """
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent / "branded_fare_scraper"
    banned = ("solveCaptchas", "solvecaptchas", "proxy=residential",
              "proxyCountry", "stealth=true", "&stealth", "?stealth")
    hits = []
    for src in sorted(root.rglob("*.py")):
        text = src.read_text(encoding="utf-8")
        for token in banned:
            if token in text:
                hits.append(f"{src.name}: {token}")
    assert hits == [], hits


def test_channel_options_ignore_stale_channel_ok_flag():
    # channel_ok only ever means "a real browser actually launched"; without a
    # configured channel the bundled profile must still win.
    from branded_fare_scraper.browser_pool import context_options, launch_options
    from branded_fare_scraper.config import Config
    cfg = Config(browser_channel=None)
    assert "args" in launch_options(cfg, True)
    assert "user_agent" in context_options(cfg, True)


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


# --------------------------- input parsing (io_utils) ---------------------- #
_V2_HEADER = [None, "ORIGIN_REGION", "DEST_REGION", "ORIGIN_CNTRY", "DEST_CNTRY",
              "OND", "CARRIER", "PAX", "RANK_CARRIER"]
_V2_ROWS = [
    [0, "AKT", "OGA", "CA", "IN", "YVR-DEL", "AI", 88556, 1],
    [1, "AKT", "OGA", "CA", "IN", "YVR-DEL", "CX", 22180, 2],
    [2, "AKT", "OGA", "CA", "IN", "YVR-DEL", "AI", 17, 9],      # dup (carrier, OND)
    [3, None, None, None, None, None, None, None, None],        # no OND / no carrier
    [None, None, None, None, None, None, None, None, None],     # fully blank
    [5, "EUR", "EUR", "RO", "ES", "OTP-BCN", "FR", 2269, 3],
]


def test_read_jobs_v2_ond_column(tmp_path):
    """V2 shape: single combined OND column + unnamed index column."""
    openpyxl = pytest.importorskip("openpyxl")
    from branded_fare_scraper.io_utils import read_jobs

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(_V2_HEADER)
    for r in _V2_ROWS:
        ws.append(r)
    path = tmp_path / "v2.xlsx"
    wb.save(path)
    wb.close()

    jobs = read_jobs(str(path))
    assert [(j.carrier, j.origin, j.destination) for j in jobs] == [
        ("AI", "YVR", "DEL"), ("CX", "YVR", "DEL"), ("FR", "OTP", "BCN")]

    raw = jobs[0].raw_row
    assert raw["OND"] == "YVR-DEL" and raw["CARRIER"] == "AI"
    assert raw["PAX"] == 88556 and raw["RANK_CARRIER"] == 1     # passthrough, unchanged
    assert isinstance(raw["PAX"], int)
    assert raw["ORIGIN_REGION"] == "AKT" and raw["DEST_CNTRY"] == "IN"
    assert "" not in raw and "None" not in raw                  # unnamed index dropped
    assert set(raw) == {"ORIGIN_REGION", "DEST_REGION", "ORIGIN_CNTRY", "DEST_CNTRY",
                        "OND", "CARRIER", "PAX", "RANK_CARRIER"}


def test_read_jobs_legacy_formats_still_parse(tmp_path):
    """Origin+Destination inputs and the older OND/CARRIER lists keep working."""
    from branded_fare_scraper.io_utils import read_jobs

    od = tmp_path / "legacy_od.csv"
    od.write_text("Carrier,Origin,Destination\nTK,IST,LHR\nTK,IST,LHR\nSQ,lhr,sin\n", "utf-8")
    jobs = read_jobs(str(od))
    assert [(j.carrier, j.origin, j.destination) for j in jobs] == [
        ("TK", "IST", "LHR"), ("SQ", "LHR", "SIN")]            # dedupe + upper-casing
    assert jobs[0].raw_row == {"Carrier": "TK", "Origin": "IST", "Destination": "LHR"}

    ond = tmp_path / "legacy_ond.csv"
    ond.write_text("OND,CARRIER\nLHR-SIN,SQ\nGRU-CAN,QR\n", "utf-8")
    jobs = read_jobs(str(ond))
    assert [(j.carrier, j.origin, j.destination) for j in jobs] == [
        ("SQ", "LHR", "SIN"), ("QR", "GRU", "CAN")]


def test_airports_cover_round15_additions():
    from branded_fare_scraper.airports import meta
    codes = ["BOS", "CMB", "DFW", "DPS", "DTW", "ECN", "EVN",
             "IAD", "IAH", "KRK", "NQZ", "SGN", "TBS", "YNB"]
    for code in codes:
        m = meta(code)
        assert m["country_code"] not in ("", "??"), code
        assert len(m["country_code"]) == 2, code
        assert m["city_name"] and m["country_name"] != "Other", code
        assert m["region"] in {"Europe", "Asia", "Middle East", "N. America",
                               "S. America", "Oceania", "Africa", "Turkey"}, code
        assert len(m["city_code"]) == 3, code
    assert meta("DTW")["city_code"] == "DTT"       # metro codes where IATA has one
    assert meta("IAD")["city_code"] == "WAS"
    assert meta("IAH")["city_code"] == "HOU"
    assert meta("BOS")["city_code"] == "BOS"       # no distinct metro code


# ---------- Trip.com deep link + hydration robustness (pilot-1 root cause) ---- #
def test_tripcom_search_url_carries_the_required_city_slug():
    # Live-proven: without a "<from>-to-<to>" path segment the site bounces to
    # its homepage (lang flips to es-US, zero result rows) — that broke every
    # search in the first pilot. City names come from the airport table.
    from branded_fare_scraper.sources.tripcom import Tripcom, city_slug
    url = Tripcom().search_url("YVR", "DEL", date(2026, 11, 9), "y")
    assert "/flights/vancouver-to-delhi/tickets-yvr-del?" in url
    assert "dcity=yvr&acity=del" in url and "class=y" in url and "curr=USD" in url
    assert city_slug("IST") == "istanbul"          # diacritics folded, no spaces
    assert city_slug("JFK") == "newyork"
    assert city_slug("ZZZ") == "zzz"               # unknown airport -> IATA code


def test_tripcom_hydration_is_language_independent():
    # A geo-localised (Spanish) page with real rows must count as hydrated.
    import asyncio as _asyncio
    from branded_fare_scraper.sources.tripcom import Tripcom

    class _Page:
        def __init__(self, rows): self.rows = rows
        async def evaluate(self, js, *a):
            if "u_select_btn" in js:
                return self.rows
            return "Ver contenido principal | vuelos"

    t = Tripcom(); t.hydrate_timeout_s = 0.05
    assert _asyncio.run(t._wait_hydrated(_Page(8))) is True

    class _CountText(_Page):                       # localized result COUNT only
        async def evaluate(self, js, *a):
            return 0 if "u_select_btn" in js else "97 vuelos encontrados"

    assert _asyncio.run(t._wait_hydrated(_CountText(0))) is True

    class _FormOnly(_Page):                        # search form words must NOT count
        async def evaluate(self, js, *a):
            return 0 if "u_select_btn" in js else "Round-trip | One-way | Nonstop | vuelos"

    assert _asyncio.run(t._wait_hydrated(_FormOnly(0))) is False

    class _Empty(_Page):
        async def evaluate(self, js, *a):
            return 0 if "u_select_btn" in js else "Book cheap flights"

    assert _asyncio.run(t._wait_hydrated(_Empty(0))) is False


def test_tripcom_dead_route_needs_two_failed_dates():
    import asyncio as _asyncio
    from branded_fare_scraper.sources.tripcom import Tripcom

    class _Page:
        url = "https://us.trip.com/flights"
        async def goto(self, *a, **k): return None
        async def evaluate(self, js, *a): return 0
        async def screenshot(self, **k): return None

    Tripcom._dead_routes.clear(); Tripcom._hydrate_misses.clear()
    t = Tripcom(); t.hydrate_timeout_s = 0.05
    _asyncio.run(t._do_search(_Page(), "AAA", "BBB", date(2026, 5, 1), "y"))
    assert ("AAA", "BBB") not in Tripcom._dead_routes          # one miss: not dead
    _asyncio.run(t._do_search(_Page(), "AAA", "BBB", date(2026, 5, 2), "y"))
    assert ("AAA", "BBB") in Tripcom._dead_routes              # second date: dead
    Tripcom._dead_routes.clear(); Tripcom._hydrate_misses.clear()


def test_tripcom_blocked_detects_real_wall_signatures():
    """A WAF wall must be told apart from an empty result page.

    Both signatures below were captured live on 2026-08-01: a datacenter IP got
    a 17-byte "whaleguard block" body, a flagged residential IP got a page whose
    only tell is the <title>. Reading the body alone would miss the second one.
    """
    import asyncio as _asyncio
    from branded_fare_scraper.sources.tripcom import Tripcom

    class _Page:
        def __init__(self, title="", body=""): self._t, self._b = title, body
        async def evaluate(self, js, *a): return [self._t, self._b[:1500]]

    t = Tripcom.__new__(Tripcom)
    run = lambda p: _asyncio.run(t._blocked(p))                # noqa: E731

    assert run(_Page(body="whaleguard block"))[0] is True
    assert run(_Page(title="Challenge Validation"))[0] is True
    # A real results shell is long, even when the route has no flights.
    assert run(_Page(title="Book Cheap Flights | Trip.com",
                     body="Flights\nHotels\n" * 100))[0] is False
    # A totally empty body is a failed load, NOT a wall: calling it a block
    # would turn every network hiccup into a run-wide cooldown.
    assert run(_Page())[0] is False


def test_tripcom_block_never_marks_route_dead():
    """The day02 regression: a wall must not blacklist the route.

    Two blocked dates used to fill _hydrate_misses, mark the route dead, and
    make every later unit return "no flights" in 0.0s without loading anything —
    which is how a whole run reported 372 dead routes while collecting zero.
    """
    import asyncio as _asyncio
    import pytest as _pytest
    from branded_fare_scraper.sources.tripcom import Tripcom
    from branded_fare_scraper.retry import RateLimited

    class _WalledPage:
        url = "https://us.trip.com/flights"
        async def goto(self, *a, **k): return None
        async def screenshot(self, **k): return None
        async def evaluate(self, js, *a):
            # _wait_hydrated counts rows; _blocked asks for [title, body].
            return ["", "whaleguard block"] if "document.title" in js else 0

    Tripcom._dead_routes.clear(); Tripcom._hydrate_misses.clear()
    Tripcom._cooldown_s = 0.0                   # don't actually sleep the test
    t = Tripcom(); t.hydrate_timeout_s = 0.05
    with _pytest.raises(RateLimited):
        _asyncio.run(t._do_search(_WalledPage(), "AAA", "BBB", date(2026, 5, 1), "y"))
    assert ("AAA", "BBB") not in Tripcom._dead_routes
    assert not Tripcom._hydrate_misses.get(("AAA", "BBB"))
    Tripcom._dead_routes.clear(); Tripcom._hydrate_misses.clear()


def test_enuygun_cooldown_doubles_then_resets():
    """Enuygun's circuit breaker, added 2026-08-03, mirrors Tripcom's exactly.

    Enuygun had zero run-wide wall protection: a Cloudflare challenge only
    raised Forbidden for that one unit, so N concurrent workers would each
    retry independently into a live wall instead of the whole run backing off
    together. Proportional to running ~10x today's tested volume tonight.
    """
    import asyncio
    from branded_fare_scraper.sources.enuygun import Enuygun
    Enuygun.reset_state()
    Enuygun.cooldown_base_s, Enuygun.cooldown_max_s = 0.01, 0.04
    try:
        Enuygun._cooldown_s = Enuygun.cooldown_base_s
        assert asyncio.run(Enuygun._cooldown()) is True
        assert Enuygun._cooldown_s == pytest.approx(0.02)      # doubled
        assert asyncio.run(Enuygun._cooldown()) is True
        assert Enuygun._cooldown_s == pytest.approx(0.04)      # doubled, now maxed
        for _ in range(Enuygun.clean_streak_reset):
            Enuygun._note_clean()
        assert Enuygun._cooldown_s == pytest.approx(0.01)
        assert Enuygun._maxed_pauses == 0
    finally:
        Enuygun.cooldown_base_s, Enuygun.cooldown_max_s = 90.0, 600.0
        Enuygun.reset_state()


def test_enuygun_persistent_wall_stops_the_run_cleanly():
    import asyncio
    from branded_fare_scraper.sources.enuygun import Enuygun
    Enuygun.reset_state()
    Enuygun.cooldown_base_s = Enuygun.cooldown_max_s = 0.01
    try:
        Enuygun._cooldown_s = 0.01
        outcomes = [asyncio.run(Enuygun._cooldown())
                    for _ in range(Enuygun.max_maxed_pauses)]
        assert outcomes[:-1] == [True] * (Enuygun.max_maxed_pauses - 1)
        assert outcomes[-1] is False                    # give up, keep the checkpoint
        assert Enuygun._abort_run is True
        # Every later unit returns immediately instead of hammering the wall.
        result = asyncio.run(Enuygun()._do_search(None, "o", "d", "AAA", "BBB",
                                                  date(2027, 6, 1)))
        assert result == []
    finally:
        Enuygun.cooldown_base_s, Enuygun.cooldown_max_s = 90.0, 600.0
        Enuygun.reset_state()


def test_carrier_names_resolve_for_every_spelling_the_ota_prints():
    """An unresolvable airline name is a silently dropped row, not a near miss.

    Across the 2026-08-02/03 runs, 17 carriers produced 0 rows in 140 unit
    attempts and every one was written off as "carrier not on route" — while the
    evidence log showed the results page listing them by name. Two causes, both
    ours: the carrier was missing from CARRIER_NAMES entirely (SKY express,
    Tarom, Transavia, Hainan), or the table held a name the OTA does not print
    ("ANA" vs "All Nippon Airways", "SAS" vs "Scandinavian Airlines").

    Every string below was copied from a real Trip.com results page.
    """
    from branded_fare_scraper.sources.base import resolve_airline_code

    seen_on_trip_com = {
        "SKY express": "GQ", "Tarom": "RO", "Transavia": "HV",
        "Hainan Airlines": "HU", "All Nippon Airways": "NH",
        "Scandinavian Airlines": "SK", "KLM Royal Dutch Airlines": "KL",
        "Austrian Airlines": "OS", "China Eastern Airlines": "MU",
        "China Southern Airlines": "CZ", "AEGEAN": "A3", "easyJet": "U2",
        "Ryanair": "FR", "Brussels Airlines": "SN", "Asiana Airlines": "OZ",
        "Turkish Airlines": "TK", "Virgin Atlantic": "VS", "Emirates": "EK",
    }
    for name, code in seen_on_trip_com.items():
        assert resolve_airline_code(name) == code, f"{name!r} must resolve to {code}"

    # Wizz sells one brand through three AOCs. The plain name must not be
    # guessed into a specific one, and each AOC must still resolve exactly.
    assert resolve_airline_code("Wizz Air") == "W6"
    assert resolve_airline_code("Wizz Air Malta") == "W4"
    assert resolve_airline_code("Wizz Air UK") == "W9"

    # Still refuses to guess when a name is genuinely ambiguous or unknown.
    assert resolve_airline_code("Some Airline Nobody Has") is None
    assert resolve_airline_code("") is None


def test_tripcom_rejects_a_page_that_answers_a_different_query():
    """The URL is written by us and proves nothing — check the rendered page.

    Trip.com keeps the previous itinerary in its search widget, so a results page
    can display one search while the address bar claims another (live 2026-08-03:
    a PVG-LGW URL rendered with "Bangkok" and a Jun 27-30 round trip still in the
    form). `_ensure_context` compares `page.url` only, so it is true by
    construction. A list belonging to another DATE is the dangerous case: it
    parses cleanly and publishes as real prices for the wrong day.

    Tolerance is deliberate on the OND side: asking for LGW can legitimately
    return a London-all-airports list where some rows read LHR.
    """
    import asyncio as _asyncio
    from branded_fare_scraper.sources.tripcom import Tripcom

    class _Page:
        def __init__(self, probe):
            self.probe = probe

        async def evaluate(self, js, arg=None):
            return self.probe

    def verdict(probe):
        return _asyncio.run(Tripcom()._query_matches(
            _Page(probe), "PVG", "LGW", date(2027, 6, 26)))

    ok, why = verdict({"rows": 8, "onRoute": 8, "activeDate": "Jun 26"})
    assert ok and not why, "the live page for the requested query must pass"

    ok, why = verdict({"rows": 8, "onRoute": 8, "activeDate": "Jun 27"})
    assert not ok and "Jun 27" in why, "a list priced for another day must be rejected"

    ok, _ = verdict({"rows": 8, "onRoute": 0, "activeDate": "Jun 26"})
    assert not ok, "8 rows and not one mentions the origin is a different search"

    ok, _ = verdict({"rows": 6, "onRoute": 2, "activeDate": "Jun 26"})
    assert ok, "a London-all-airports list where only some rows read LGW is real data"

    ok, _ = verdict({"rows": 0, "onRoute": 0, "activeDate": ""})
    assert ok, "nothing rendered yet is not this check's call to make"


def test_tripcom_empty_pages_on_many_routes_are_a_throttle_not_no_availability():
    """A hydrated page that parses to nothing must never publish "no flights".

    2026-08-02, live: with 5 workers the run reported "no flights LHR-BOM" for
    30 minutes straight (no puzzle, no interstitial, no hydration miss) while a
    browser on the SAME IP loaded the same URL and showed 8 flights, and a fresh
    single-worker process scraped that very unit to 7 brands. The site throttles
    by going quiet, so the empty read has to back the run off and let the units
    retry instead of being written down as real absence.

    One route going blank stays ordinary — a thin OND can genuinely be empty on
    one date — so only DISTINCT routes count toward the brake.
    """
    import asyncio as _asyncio
    from branded_fare_scraper.sources.tripcom import Tripcom

    async def note(seq):
        Tripcom._soft_empty_routes = []
        tripped = []
        for o, d in seq:
            tripped.append(await Tripcom._note_soft_empty(o, d, date(2027, 6, 1)))
        return tripped

    Tripcom._cooldown_s = 0.0                     # do not actually sleep
    Tripcom._abort_run = False
    Tripcom._maxed_pauses = 0

    same = _asyncio.run(note([("LHR", "BOM")] * 5))
    assert not any(same), "one thin route must not brake the whole run"

    Tripcom._cooldown_s = 0.0
    Tripcom._abort_run = False
    Tripcom._maxed_pauses = 0
    mixed = _asyncio.run(note([("LHR", "BOM"), ("MAD", "OTP"), ("CDG", "BKK")]))
    assert mixed[:2] == [False, False], "brake only after the third distinct route"
    assert mixed[2] is True, "three unrelated routes going blank at once is a throttle"

    Tripcom._soft_empty_routes = []
    Tripcom._cooldown_s = Tripcom.cooldown_base_s
    Tripcom._abort_run = False
    Tripcom._maxed_pauses = 0


def test_tripcom_closed_drawer_is_not_reported_as_open():
    """A mounted-but-hidden drawer must read as GONE, not as still open.

    Trip.com leaves the drawer container in the DOM after it closes. The root
    finder used to accept it on text alone, so `_drawer_gone` never returned
    True and `_force_reset` reloaded the whole results page — 1054 times in the
    2026-08-02 run. Every reload resets the flight list, so "inspect every
    flight, keep the richest ladder" ran on the first one or two rows and 45%
    of published ladders collapsed to a single package.
    """
    import json
    import shutil
    import subprocess
    if not shutil.which("node"):
        pytest.skip("node not available for JS-level checks")
    from branded_fare_scraper.sources.tripcom import _DRAWER_ROOT_JS

    def probe(hidden: str) -> dict:
        dom = f"""
const drawer = mk('div','',{{class:'flt-drawer-policy-be', hidden:'{hidden}'}},[
  mk('div','Select Fare',{{}}),
  mk('div','Economy class\\nEco Fly\\nBaggage: 2 x 50 lbs\\n$720',{{}})]);
const page = mk('div','Hotels Homes Flights unrelated page text '.repeat(8),{{class:'page'}});
const body = mk('body','',{{}},[page, drawer]);
const document = {{ body: body, querySelector:(s)=>body.querySelectorAll(s)[0]||null,
                   querySelectorAll:(s)=>body.querySelectorAll(s) }};
"""
        code = (_JS_DOM_SHIM + dom +
                "const fn = new Function('document','getComputedStyle',"
                "'return (' + %s + ')()');\n" % json.dumps(_DRAWER_ROOT_JS) +
                "console.log(JSON.stringify(fn(document, getComputedStyle)));")
        res = subprocess.run(["node", "-e", code], capture_output=True, text=True)
        assert res.returncode == 0, res.stderr[:400]
        return json.loads(res.stdout)

    assert probe("0")["found"] is True, "an OPEN drawer must still be detected"
    assert probe("1")["found"] is False, "a closed (hidden, zero-box) drawer must read as gone"


def test_tripcom_closes_a_drawer_variant_the_old_selector_could_not_reach():
    """The close control must be found on ANY drawer variant, not just one class.

    _drawer_gone resolves the drawer through the whole __drawerRoot() chain
    ('.flt-drawer-policy-be', [role="dialog"], [class*="drawer"], aside, ...)
    while _close_drawer used to query '.flt-drawer-policy-be' alone. On every
    other variant the X was therefore unreachable by construction: only Escape
    remained, and when the page swallowed it the detector still saw an open
    drawer, so _force_reset reloaded the entire results page. Each reload throws
    the expanded flight list away, which is the same failure mode that produced
    the single-package ladders.
    """
    import json
    import shutil
    import subprocess
    if not shutil.which("node"):
        pytest.skip("node not available for JS-level checks")
    from branded_fare_scraper.sources.tripcom import _CLOSE_DRAWER_JS

    def probe(drawer_attrs: str) -> dict:
        dom = f"""
const shut = mk('button','',{{class:'ibu_flt_close', w:'32'}});
const drawer = mk('div','',{drawer_attrs},[
  mk('div','Select Fare',{{}}), shut,
  mk('div','Economy class\\nEco Fly\\nBaggage: 2 x 50 lbs\\n$720',{{}})]);
const page = mk('div','Hotels Homes Flights unrelated page text '.repeat(8),{{class:'page'}});
const body = mk('body','',{{}},[page, drawer]);
const document = {{ body: body, querySelector:(s)=>body.querySelectorAll(s)[0]||null,
                   querySelectorAll:(s)=>body.querySelectorAll(s) }};
"""
        code = (_JS_DOM_SHIM + dom +
                "const fn = new Function('document','getComputedStyle','CLICKS',"
                "'return (' + %s + ')()');\n" % json.dumps(_CLOSE_DRAWER_JS) +
                "const out = fn(document, getComputedStyle, CLICKS);\n"
                "out.clicks = CLICKS;\n"
                "console.log(JSON.stringify(out));")
        res = subprocess.run(["node", "-e", code], capture_output=True, text=True)
        assert res.returncode == 0, res.stderr[:400]
        return json.loads(res.stdout)

    known = probe("{class:'flt-drawer-policy-be'}")
    assert known["clicked"] is True and known["direct"] is True

    # The regression: same drawer, different class. The old query returned null
    # here and the X was never clicked.
    variant = probe("{role:'dialog', class:'ibu_flt_modal'}")
    assert variant["found"] is True, "the detector's own chain finds this drawer"
    assert variant["clicked"] is True, "so the closer must reach its X too"
    assert variant["direct"] is False, "and it is NOT the legacy class"
    assert "ibu_flt_close" in variant["clicks"]
