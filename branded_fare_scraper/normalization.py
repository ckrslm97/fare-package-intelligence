"""Brand-name normalization + *true* hierarchy ordering.

The spec is explicit: do **not** trust screen order alone. Normalize the raw
brand name to a canonical sub-tier and derive a rank, so packages line up as
Lite -> Basic -> Standard -> Comfort -> Flex -> Premium regardless of how the
site happened to render them. Different airlines use different names, so the
token tables below cover common (and some carrier-specific) variants.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from .models import Cabin, PriceType, RawBrand

# Cabin base offsets keep cabins globally ordered (all Economy < all Business).
CABIN_BASE = {
    Cabin.ECONOMY: 0,
    Cabin.PREMIUM_ECONOMY: 1000,
    Cabin.BUSINESS: 2000,
    Cabin.FIRST: 3000,
}

# Cabin -> tier-code prefix (Eco-1, PEco-1, Bus-1, Fir-1) as in the reference image.
CABIN_TIER_PREFIX = {
    Cabin.ECONOMY: "Eco",
    Cabin.PREMIUM_ECONOMY: "PEco",
    Cabin.BUSINESS: "Bus",
    Cabin.FIRST: "Fir",
}


def tier_code(cabin: Cabin, order_in_cabin: int) -> str:
    """Eco-1, Eco-2, PEco-1, Bus-1 … (1-based within the cabin)."""
    return f"{CABIN_TIER_PREFIX.get(cabin, 'Eco')}-{order_in_cabin + 1}"

# Canonical sub-tiers, cheapest -> most flexible. (rank, label)
SUBTIER_ORDER = [
    ("lite", 10, "Lite"),
    ("basic", 20, "Basic"),
    ("standard", 30, "Standard"),
    ("comfort", 40, "Comfort"),
    ("flex", 50, "Flex"),
    ("premium", 60, "Premium"),
]
SUBTIER_RANK = {k: r for k, r, _ in SUBTIER_ORDER}
SUBTIER_LABEL = {k: lbl for k, _, lbl in SUBTIER_ORDER}

# Tokens -> sub-tier key. Longer/brand-specific tokens are checked first so that
# e.g. "ecofly" wins over the generic "eco".
SUBTIER_TOKENS: list[tuple[str, str]] = [
    # carrier-specific brand words (checked first)
    ("primefly", "premium"), ("businessprime", "premium"), ("flexfly", "flex"),
    ("extrafly", "standard"), ("ecofly", "basic"), ("ecojet", "basic"),
    ("businessflex", "flex"), ("businessclassic", "standard"),
    # generic
    ("light", "lite"), ("lite", "lite"), ("hafif", "lite"),
    ("basic", "basic"), ("basis", "basic"), ("saver", "basic"), ("promo", "basic"),
    ("value", "basic"), ("go ", "basic"), ("economy go", "basic"), ("eco ", "basic"),
    ("standard", "standard"), ("classic", "standard"), ("smart", "standard"),
    ("main", "standard"), ("semiflex", "standard"),
    ("comfort", "comfort"), ("plus", "comfort"), ("advantage", "comfort"),
    ("flex", "flex"), ("flexible", "flex"), ("esnek", "flex"),
    ("premium", "premium"), ("prime", "premium"), ("full", "premium"),
    ("max", "premium"), ("top", "premium"),
]

# Cabin tokens for when the cabin is not already known from structured data.
# Premium-Economy tokens MUST precede the generic "eco"/"economy"/"business"
# tokens: e.g. "PREMECON" contains "eco", and "premium ekonomi" contains
# "ekonomi", so without these first they would misclassify as Economy.
CABIN_TOKENS: list[tuple[str, Cabin]] = [
    ("premium economy", Cabin.PREMIUM_ECONOMY), ("premium eco", Cabin.PREMIUM_ECONOMY),
    ("premium ekonomi", Cabin.PREMIUM_ECONOMY), ("prem economy", Cabin.PREMIUM_ECONOMY),
    ("prem eco", Cabin.PREMIUM_ECONOMY), ("prem econ", Cabin.PREMIUM_ECONOMY),
    ("premecon", Cabin.PREMIUM_ECONOMY), ("world traveller plus", Cabin.PREMIUM_ECONOMY),
    ("premium select", Cabin.PREMIUM_ECONOMY),
    ("business", Cabin.BUSINESS), ("busines", Cabin.BUSINESS), ("işletme", Cabin.BUSINESS),
    ("club", Cabin.BUSINESS), ("upper", Cabin.BUSINESS),
    ("first", Cabin.FIRST), ("suite", Cabin.FIRST),
    ("economy", Cabin.ECONOMY), ("ekonomi", Cabin.ECONOMY), ("eco", Cabin.ECONOMY),
    ("coach", Cabin.ECONOMY), ("main", Cabin.ECONOMY),
]

# Fare-family codes for Premium Economy that carry no cabin word in the brand
# name (e.g. British Airways "PESEL"/"PEPRO"/"PEFLEX"/"PREMECON"). Matched as a
# whole token so real Economy/Business codes are unaffected.
_PE_CODE_RE = re.compile(r"^PE[A-Z]{2,}$")

# Carrier-specific Premium-Economy fare-family codes that are too short/ambiguous
# to match globally (live-verified on Ubfly: AC DEL-YVR economy ladder carries
# ff "PL-PL" = Premium Economy Lowest and "PF-PF" = Premium Economy Flexible,
# the latter mislabeled "PRIME FLY" by the OTA).
PE_FF_BY_CARRIER: dict[str, set[str]] = {
    "AC": {"PL", "PF", "PB"},
}

# Authoritative per-carrier fare-family table: code -> (display name, cabin).
# Ubfly's code->name dictionary mislabels these AC codes (PF rendered as TK's
# "PRIME FLY", EL as "Economy Light", EF as "ECO FLY", PL/PB/EB as the bare
# code), while the CODE itself is reliable — verified live via search cabin,
# onclick cabin arg and price-ladder position. For these codes the table
# decides both display name and cabin.
CARRIER_FF_BRANDS: dict[str, dict[str, tuple[str, Cabin]]] = {
    "AC": {
        "PB": ("Premium Economy Basic", Cabin.PREMIUM_ECONOMY),
        "PL": ("Premium Economy Lowest", Cabin.PREMIUM_ECONOMY),
        "PF": ("Premium Economy Flexible", Cabin.PREMIUM_ECONOMY),
        "EB": ("Business Basic", Cabin.BUSINESS),
        "EL": ("Business Lowest", Cabin.BUSINESS),
        "EF": ("Business Flexible", Cabin.BUSINESS),
    },
}


def ff_override(carrier: Optional[str], ffcode: Optional[str]) -> Optional[tuple[str, Cabin]]:
    """(display name, cabin) for a carrier's fare-family code, if tabled."""
    tab = CARRIER_FF_BRANDS.get((carrier or "").upper())
    if not tab:
        return None
    for t in _ff_tokens(ffcode or ""):
        if t in tab:
            return tab[t]
    return None


# Carrier display names for brands the OTA renders in the wrong case or as a
# raw token ("ECOFLY", "BUSINESS PRIME", "BCLASSIC"). Keyed by the collapsed
# name (brand_match_key). Only spelling/casing — never invents a brand.
CARRIER_BRAND_SPELLING: dict[str, dict[str, str]] = {
    "TK": {
        "ecofly": "EcoFly", "extrafly": "ExtraFly", "flexfly": "FlexFly",
        "primefly": "PrimeFly", "businessfly": "Business Fly",
        "businessprime": "Business Prime", "businessflex": "Business Flex",
    },
    "QR": {
        "blite": "Business Lite", "bclassic": "Business Classic",
        "bcomfort": "Business Comfort", "belite": "Business Elite",
    },
    # Compressed OTA family tokens expanded to the airline's own brand wording.
    "EK": {"ecosaver": "Economy Saver", "ecoflex": "Economy Flex",
           "ecoflxplus": "Economy Flex Plus", "bssaver": "Business Saver",
           "bsflex": "Business Flex", "bsflxplus": "Business Flex Plus",
           "pyflxplus": "Premium Economy Flex Plus"},
    "EY": {"ybasic": "Economy Basic", "yvalue": "Economy Value",
           "ycomfort": "Economy Comfort", "ydeluxe": "Economy Deluxe",
           "jvalue": "Business Value", "jcomfort": "Business Comfort",
           "jdeluxe": "Business Deluxe"},
    "AI": {"ecovalu": "Economy Value", "ecoclas": "Economy Classic",
           "ecoflx": "Economy Flex", "busvalu": "Business Value",
           "busflx": "Business Flex", "peyvalu": "Premium Economy Value",
           "peyflx": "Premium Economy Flex"},
    "DL": {"mainbasic": "Main Basic", "mainclasc": "Main Classic",
           "mainextra": "Main Extra", "comftclasc": "Comfort Classic",
           "comftextra": "Comfort Extra", "dpsclasc": "Premium Select Classic",
           "dpsextra": "Premium Select Extra", "doneclasc": "Delta One Classic",
           "doneextra": "Delta One Extra"},
    "LH": {"ecobasepl": "Economy Basic Plus", "ecocmft": "Economy Comfort",
           "ecocmftpls": "Economy Comfort Plus", "ecoflex": "Economy Flex",
           "prelight": "Premium Economy Light", "precmft": "Premium Economy Comfort",
           "precmftpls": "Premium Economy Comfort Plus", "preflex": "Premium Economy Flex",
           "pregreic": "Premium Economy Green", "buslight": "Business Light",
           "buscmft": "Business Comfort", "buscmftpls": "Business Comfort Plus",
           "busgreic": "Business Green"},
    "AF": {"lightbag": "Light", "standard3": "Standard", "flex1": "Flex",
           "premstand": "Premium Standard", "premflex": "Premium Flex"},
    "KL": {"bizlight": "Business Light", "premlight": "Premium Light",
           "premstand": "Premium Standard", "premflex": "Premium Flex"},
    "GF": {"ecolite": "Economy Lite", "ecosmart": "Economy Smart",
           "ecoflex": "Economy Flex", "bizsmart": "Business Smart"},
    "UA": {"premeco": "Premium Economy", "premecoref": "Premium Economy Refundable",
           "premecoprf": "Premium Economy Part Refundable",
           "busref": "Business Refundable", "buspartref": "Business Part Refundable"},
    "BA": {"econsel": "Economy Select", "econpro": "Economy Pro",
           "econflex": "Economy Flex", "bizpromo": "Business Promo",
           "bizsel": "Business Select", "bizpro": "Business Pro",
           "bizflex": "Business Flex"},
}

_BARE_CODE_RE = re.compile(r"^[A-Z]{2,3}\d?$")

# Ordinary fare words: a single ALL-CAPS token matching one of these is safe to
# capitalize ("FLEXIBLE" -> "Flexible") — unlike opaque family tokens (PREMECON).
_FARE_WORDS = {"flexible", "standard", "comfort", "latitude", "basic", "economy",
               "business", "flex", "classic", "saver", "value", "ultimate",
               "premium", "lite", "light", "smart", "prime", "restricted",
               "promo", "plus", "elite", "convenience"}


def pretty_brand_name(name: str, carrier: Optional[str] = None) -> str:
    """Human display form of a brand name — casing/spelling only.

    Carrier spelling table first; then ALL-CAPS names become Title Case
    ("ECONOMY CLASSIC" -> "Economy Classic"). Bare code-like names (e.g. "BX")
    are returned unchanged — they are surfaced by the suspicious-name scan
    instead of being guessed at.
    """
    s = (name or "").strip()
    if not s:
        return s
    tab = CARRIER_BRAND_SPELLING.get((carrier or "").upper(), {})
    hit = tab.get(brand_match_key(s))
    if hit:
        return hit
    if _BARE_CODE_RE.match(s):
        return s
    # Generic fix for MULTI-word ALL-CAPS ("ECONOMY CLASSIC") and for single
    # ALL-CAPS tokens that are ordinary fare words ("FLEXIBLE"). Unknown
    # single-token caps (PREMECON) stay as-is: guessing "Premecon" would be
    # worse; they surface in the suspicious-name report to be tabled.
    if s.isupper() and " " in s:
        return " ".join(w.capitalize() for w in s.split())
    if s.isupper() and s.lower() in _FARE_WORDS:
        return s.capitalize()
    return s


def is_suspicious_brand_name(name: str, ffcode: Optional[str] = None) -> bool:
    """True for names that look like raw codes rather than real brand names.

    A name equal to its fare-family token is only suspicious when it LOOKS like
    a compressed token (single ALL-CAPS word outside the ordinary fare
    vocabulary) — "Basic"/"Flex" matching their own codes are fine.
    """
    s = (name or "").strip()
    if not s:
        return True
    if _BARE_CODE_RE.match(s):
        return True
    if not (s.isupper() and " " not in s and s.lower() not in _FARE_WORDS):
        return False
    toks = _ff_tokens(ffcode or "")
    return bool(toks) and brand_match_key(s) == brand_match_key(toks[0])


def _looks_pe_code(token: str) -> bool:
    return bool(_PE_CODE_RE.match((token or "").strip().upper()))


def _ff_tokens(ffcode: str) -> list[str]:
    return [t for t in re.split(r"[-_/ ]", (ffcode or "").upper()) if t]


def effective_cabin(name: str, ffcode: Optional[str], hint: Optional[Cabin],
                    fallback: Cabin, carrier: Optional[str] = None) -> Cabin:
    """Best guess at a brand's true cabin from its name/fare-family code.

    Priority: an explicit cabin word in the name → a PE fare-family code
    (global ``PE…`` pattern, or the carrier-specific short codes above) →
    a structured hint (e.g. the site's onclick cabin arg) → the fallback
    (the cabin we searched). This lets us re-bucket fares that a source
    mislabels (an "Economy Light" upsell served inside a Business search).
    """
    ov = ff_override(carrier, ffcode) or ff_override(carrier, name)
    if ov:
        return ov[1]
    per_carrier = PE_FF_BY_CARRIER.get((carrier or "").upper(), set())
    if per_carrier and any(t in per_carrier for t in _ff_tokens(ffcode or "") + _ff_tokens(name)):
        return Cabin.PREMIUM_ECONOMY
    cab = detect_cabin(name)
    if cab:
        return cab
    if _looks_pe_code(name) or _looks_pe_code(ffcode or ""):
        return Cabin.PREMIUM_ECONOMY
    return hint or fallback


def _clean(s: str) -> str:
    s = re.sub(r"\bclass\b|\bfare\b|\bpackage\b|\bbrand\b", " ", s or "", flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", s).strip()


def detect_cabin(text: str) -> Optional[Cabin]:
    t = f" {(text or '').lower()} "
    for token, cab in CABIN_TOKENS:
        if token in t:
            return cab
    return None


def detect_subtier(raw_name: str) -> Optional[str]:
    t = f" {(raw_name or '').lower()} "
    for token, tier in SUBTIER_TOKENS:
        if token.strip() and token in t:
            return tier
    return None


@dataclass
class NormalizedBrand:
    normalized_name: str
    subtier: Optional[str]
    rank: int
    matched: bool          # whether the sub-tier was recognized (vs. fallback)


def normalize_brand(raw_name: str, cabin: Optional[Cabin] = None) -> NormalizedBrand:
    """Normalize one raw brand name to ``<Cabin> <SubTier>`` + a global rank."""
    cab = cabin or detect_cabin(raw_name) or Cabin.ECONOMY
    subtier = detect_subtier(raw_name)
    base = CABIN_BASE.get(cab, 0)
    if subtier:
        rank = base + SUBTIER_RANK[subtier]
        name = f"{cab.value} {SUBTIER_LABEL[subtier]}"
        return NormalizedBrand(name, subtier, rank, True)
    # Fallback: unknown sub-tier -> keep cleaned raw, park in the middle.
    cleaned = _clean(raw_name) or cab.value
    return NormalizedBrand(f"{cab.value} — {cleaned}", None, base + 35, False)


def _resolved_prices(brands: list[RawBrand]) -> list[Optional[float]]:
    """Absolute price per brand, resolving deltas from the base anchor.

    Deltas are only meaningful in screen (hierarchy) order, so we resolve there
    and map the results back to the input order. All current adapters already
    emit ``ABSOLUTE`` prices, in which case this is an identity pass.
    """
    if all(b.price_type == PriceType.ABSOLUTE for b in brands):
        return [b.price_value for b in brands]
    from .pricing import compute_absolute_prices  # local import avoids an import cycle
    order = sorted(range(len(brands)), key=lambda i: brands[i].screen_order)
    resolved_in_screen = compute_absolute_prices([brands[i] for i in order])
    out: list[Optional[float]] = [None] * len(brands)
    for pos, i in enumerate(order):
        out[i] = resolved_in_screen[pos]
    return out


def assign_brand_order(brands: list[RawBrand]) -> list[tuple[RawBrand, NormalizedBrand, int]]:
    """Return brands ranked into their true hierarchy for one cabin.

    Primary sort key = **absolute price** — airline sites present a cabin's
    branded ladder cheapest-first, so price is the ground truth for ordering
    (and for the dashboard's per-tier "geçiş" deltas). Name-derived rank and
    screen order only break ties. A leading cabin-base offset keeps cabins from
    interleaving if a mixed list is ever passed. Returns ``(raw, normalized,
    order)`` with ``order`` a 0-based index within the cabin.
    """
    prices = _resolved_prices(brands)
    enriched = []
    for b, price in zip(brands, prices):
        nb = normalize_brand(b.raw_brand_name, b.cabin)
        enriched.append((b, nb, price))

    def sort_key(item):
        raw, nb, price = item
        p = price if price is not None else float("inf")
        return (CABIN_BASE.get(raw.cabin, 0), p, nb.rank, raw.screen_order)

    enriched.sort(key=sort_key)
    return [(raw, nb, i) for i, (raw, nb, _p) in enumerate(enriched)]


def regroup_brands_by_cabin(brands: list[RawBrand], group_cabin: Cabin,
                            keep_pe: bool = True,
                            carrier: Optional[str] = None) -> dict[Cabin, list[RawBrand]]:
    """Re-bucket brands to their *effective* cabin, dropping contradictions.

    A source can list a fare from another cabin inside a search (an "Economy
    Light" upsell in a Business search, or a Premium-Economy family leaking into
    the Economy search). Policy: keep a brand only if its effective cabin equals
    the searched/group cabin, or — when ``keep_pe`` — if it is Premium Economy
    (a legitimate reclassification). Anything else is noise and is dropped.
    Mutates each kept brand's ``cabin`` to the effective value.
    """
    out: dict[Cabin, list[RawBrand]] = {}
    for b in brands:
        ov = ff_override(carrier, b.fare_family_code) or ff_override(carrier, b.raw_brand_name)
        if ov:
            b.raw_brand_name = ov[0]     # the OTA's display name for this code is junk
        eff = effective_cabin(b.raw_brand_name, b.fare_family_code, b.cabin,
                              group_cabin, carrier=carrier)
        if eff == group_cabin or (keep_pe and eff == Cabin.PREMIUM_ECONOMY):
            b.cabin = eff
            out.setdefault(eff, []).append(b)
        # else: cabin contradicts the search -> drop as mislabeled noise
    return out


def iter_ranked_by_cabin(brands: list[RawBrand], group_cabin: Cabin,
                         carrier: Optional[str] = None):
    """Regroup ``brands`` to their effective cabin, then within each cabin yield
    ``(effective_cabin, raw, normalized, order, absolute_price)`` in price order.

    Single source of truth for every exporter (runner, reprocess, to_platform,
    make_excel) so ordering, tier codes and cabin re-bucketing stay identical.
    """
    from .pricing import compute_absolute_prices  # local import avoids an import cycle
    for eff_cabin, bs in regroup_brands_by_cabin(brands, group_cabin, carrier=carrier).items():
        for b in bs:                       # display casing/spelling (UI'da düzgün yazım)
            b.raw_brand_name = pretty_brand_name(b.raw_brand_name, carrier)
        # Rows whose display name is still a bare GDS code (TK "BX"/"BB" on thin
        # routes) never reach the published outputs; raw JSONL keeps them.
        bs = [b for b in bs if not _BARE_CODE_RE.match(b.raw_brand_name)]
        if not bs:
            continue
        ranked = assign_brand_order(bs)
        abs_prices = compute_absolute_prices([raw for raw, _, _ in ranked])
        for (raw, nb, order), absp in zip(ranked, abs_prices):
            yield eff_cabin, raw, nb, order, absp


_HEX_ID_RE = re.compile(r"^[0-9a-f]{16,}$", re.IGNORECASE)


def clean_fare_code(code: Optional[str]) -> str:
    """Human-friendly fare-family code for display/export.

    OTAs sometimes smuggle internal IDs into the code slot (Ubfly:
    ``CL-f48780cd96ac4e33…``) or duplicate the token (``EF-EF``). Drop long hex
    ID tokens and collapse duplicated tokens; raw data keeps the original.
    """
    toks = [t for t in re.split(r"[-_/ ]", (code or "").strip()) if t]
    toks = [t for t in toks if not _HEX_ID_RE.match(t)]
    if toks and all(t == toks[0] for t in toks):
        toks = toks[:1]
    return "-".join(toks)


# --------------------------------------------------------------------------- #
# Cross-source helpers: brand-name matching, ladder quality, enrichment
# --------------------------------------------------------------------------- #
def brand_match_key(name: str) -> str:
    """Collapse a brand name for cross-source matching: 'Eco Fly' == 'ECOFLY'."""
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def match_brands(primary: list[RawBrand], other: list[RawBrand]) -> dict[int, RawBrand]:
    """Map primary-brand index -> matching other-source brand.

    Exact collapsed-name match first; then a *unique* containment match
    ('flex' ⊂ 'flexible'); finally, if both ladders have the same length,
    unmatched slots pair up by price order.
    """
    okeys = [(brand_match_key(o.raw_brand_name), o) for o in other]
    used: set[int] = set()
    out: dict[int, RawBrand] = {}
    for i, p in enumerate(primary):
        pk = brand_match_key(p.raw_brand_name)
        if not pk:
            continue
        exact = [j for j, (ok, _) in enumerate(okeys) if ok == pk and j not in used]
        cand = exact or [j for j, (ok, _) in enumerate(okeys)
                         if ok and j not in used and (pk in ok or ok in pk)]
        if len(cand) == 1 or (cand and exact):
            out[i] = okeys[cand[0]][1]
            used.add(cand[0])
    if len(primary) == len(other):
        p_sorted = sorted(range(len(primary)),
                          key=lambda i: primary[i].price_value if primary[i].price_value is not None else float("inf"))
        o_sorted = sorted(range(len(other)),
                          key=lambda j: other[j].price_value if other[j].price_value is not None else float("inf"))
        for pi, oj in zip(p_sorted, o_sorted):
            if pi not in out and oj not in used:
                out[pi] = other[oj]
                used.add(oj)
    return out


def ladder_metrics(brands: list[RawBrand]) -> tuple[int, float]:
    """(implausible_steps, max_step_ratio) over price-sorted absolute prices.

    A step where the next fare costs >3× the previous one is 'implausible' —
    a proxy for GDS full-fare rows that the airline's own site never shows.
    """
    prices = sorted(b.price_value for b in brands if b.price_value)
    bad, worst = 0, 1.0
    for a, b in zip(prices, prices[1:]):
        if a > 0:
            r = b / a
            worst = max(worst, r)
            if r > 3.0:
                bad += 1
    return bad, round(worst, 3)


def enrich_brands(primary: list[RawBrand], other: list[RawBrand]) -> int:
    """Fill primary brands' MISSING amenity keys + miles from a matching brand
    of another source. Explicit statuses on the primary are never overridden;
    an UNKNOWN status is upgraded when the other source knows better.
    Returns the number of fields filled (for logging/tests).
    """
    from .models import AmenityStatus  # local import: keep module deps light
    filled = 0
    for i, ob in match_brands(primary, other).items():
        pb = primary[i]
        have = {a.canonical_key: a for a in pb.amenities if a.canonical_key}
        for oa in ob.amenities:
            k = oa.canonical_key
            if not k or oa.status == AmenityStatus.UNKNOWN:
                continue
            pa = have.get(k)
            if pa is None:
                pb.amenities.append(oa)
                have[k] = oa
                filled += 1
            elif pa.status == AmenityStatus.UNKNOWN:
                pa.status = oa.status
                pa.raw_value = oa.raw_value or pa.raw_value
                filled += 1
        if pb.miles.mileage_available is None and ob.miles.mileage_available is not None:
            pb.miles = ob.miles
            filled += 1
    return filled
