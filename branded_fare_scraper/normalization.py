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
from datetime import date
from itertools import combinations
from typing import Optional

from .models import Cabin, PriceType, RawBrand

#: Carriers with no Business-cabin branded-fare product to find in the first
#: place — a Business-cabin gap for one of these is not a scrape defect.
#: Single source of truth: exporters (to_platform.py) and the scrape-time
#: completeness check (validation.py) both key off this set, so "should this
#: carrier have Business?" is answered identically at report time and at
#: retry-decision time.
LCC_CARRIERS = {"PC", "VF", "TR", "U2", "FR", "W6", "W4", "W9", "G9", "J9",
                "6E", "XY", "ZF", "NO", "TU", "FZ", "4S"}

# Cabin base offsets keep cabins globally ordered (all Economy < all Business).
CABIN_BASE = {
    Cabin.ECONOMY: 0,
    Cabin.PREMIUM_ECONOMY: 1000,
    Cabin.BUSINESS: 2000,
    Cabin.FIRST: 3000,
}


def cabin_rank(cabin: Optional[Cabin]) -> int:
    """Global cabin ordering value: Economy < Premium Economy < Business < First."""
    return CABIN_BASE.get(cabin, 0)


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
    ("semiflex", 45, "Semi Flex"),
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
    ("value", "basic"),
    ("standard", "standard"), ("classic", "standard"), ("smart", "standard"),
    ("main", "standard"),
    # Semi-flex is its OWN tier between comfort and flex — must be tested
    # before the bare "flex" token below or it would rank as full flex.
    ("semi-flex", "semiflex"), ("semi flex", "semiflex"), ("semiflex", "semiflex"),
    ("deluxe", "premium"),
    ("comfort", "comfort"), ("plus", "comfort"), ("advantage", "comfort"),
    ("flex", "flex"), ("flexible", "flex"), ("esnek", "flex"),
    ("premium", "premium"), ("prime", "premium"), ("full", "premium"),
    ("max", "premium"), ("top", "premium"),
    # Cabin-prefix fallbacks LAST: "Eco Classic" is a standard fare whose first
    # word is just the cabin, so a real family word must always win over these.
    ("go ", "basic"), ("economy go", "basic"), ("eco ", "basic"),
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
    # Royal Jordanian partner-plated single-fare boxes: Ubfly labels the
    # YDELUXE box "Basic"; the code decides name and cabin.
    "RJ": {
        "YDELUXE": ("Economy Deluxe", Cabin.ECONOMY),
        "JDELUXE": ("Business Deluxe", Cabin.BUSINESS),
    },
    # Saudia guest fares: Ubfly's dictionary labels the NFLEXE box "Basic".
    "SV": {
        "NBASICE": ("Economy Basic", Cabin.ECONOMY),
        "NSEMIFLEXE": ("Economy Semi Flex", Cabin.ECONOMY),
        "NFLEXE": ("Economy Flex", Cabin.ECONOMY),
        "NBASICB": ("Business Basic", Cabin.BUSINESS),
        "NSEMIFLEXB": ("Business Semi Flex", Cabin.BUSINESS),
        "NFLEXB": ("Business Flex", Cabin.BUSINESS),
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
           "pyflxplus": "Premium Economy Flex Plus",
           "premiumeconomyflexplus": "Premium Economy Flex Plus"},
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
           "busgreic": "Business Green", "ecogreic": "Economy Green",
           "ecmftflxi": "Economy Comfort Flex", "buscmtflx": "Business Comfort Flex",
           "precmftflx": "Premium Economy Comfort Flex"},
    "AF": {"lightbag": "Light", "standard3": "Standard", "flex1": "Flex",
           "premstand": "Premium Standard", "premflex": "Premium Flex",
           "premlight": "Premium Light", "premlightb": "Premium Light",
           "bizlight": "Business Light"},
    "KL": {"bizlight": "Business Light", "premlight": "Premium Light",
           "premstand": "Premium Standard", "premflex": "Premium Flex",
           "lightbag": "Light", "standard3": "Standard", "flex1": "Flex",
           "premlightb": "Premium Light"},
    # ITA Airways now files LH-group style family tokens
    "AZ": {"ecocmft": "Economy Comfort", "ecocmftpls": "Economy Comfort Plus",
           "ecoflex": "Economy Flex", "precmft": "Premium Economy Comfort",
           "precmftpls": "Premium Economy Comfort Plus", "preflex": "Premium Economy Flex",
           "buscmft": "Business Comfort", "buscmftpls": "Business Comfort Plus"},
    "GQ": {"bliss": "Bliss", "blissplus": "Bliss Plus", "enjoy": "Enjoy",
           "joyplus": "Joy Plus", "joy": "Joy"},
    "J2": {"budget": "Budget"},
    "PC": {"avantaj": "Avantaj"},
    "RJ": {"buvalue": "Business Value", "busaver": "Business Saver",
           "bssaver": "Business Saver", "buflex": "Business Flex",
           "jdeluxe": "Business Deluxe", "ydeluxe": "Economy Deluxe",
           "ecflex": "Economy Flex", "ecsaver": "Economy Saver",
           "ecvalue": "Economy Value", "ecoflex": "Economy Flex"},
    # Business fare families surfaced by the upgrade-leak capture (v12) —
    # compressed OTA tokens -> the airlines' own brand wording.
    "AA": {"fbus": "Flagship Business"},
    "CA": {"stdbiz": "Business Standard", "flexbiz": "Business Flex",
           "ltbiz": "Business Lite"},
    "CX": {"bizlight": "Business Light", "bizessent": "Business Essential",
           "bclassic": "Business Classic"},
    "FB": {"bexecutive": "Business Executive"},
    "HO": {"flexbiz": "Business Flex"},
    "KE": {"prstandard": "Prestige Standard", "prplus": "Prestige Plus"},
    "NH": {"bizclassic": "Business Classic", "bizstd": "Business Standard",
           "bizval": "Business Value", "bizvalpls": "Business Value Plus"},
    "OZ": {"prstandard": "Prestige Standard", "prplus": "Prestige Plus"},
    "PR": {"busvalue": "Business Value"},
    "TG": {"bufl": "Business Flexible"},
    "VS": {"upper": "Upper Class"},
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
               "promo", "promotional", "plus", "elite", "convenience"}


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
    normalized_name: str   # PUBLISHED name (airline wording once ranked, see below)
    subtier: Optional[str]
    rank: int
    matched: bool          # whether the sub-tier was recognized (vs. fallback)
    #: The canonical "<Cabin> <SubTier>" label. Used for ordering/tiering only —
    #: several distinct families share one canonical label ("Economy Comfort"
    #: and "Economy Comfort Green" are both comfort), so publishing it merged
    #: real products into one row.
    canonical_name: str = ""


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


# --------------------------------------------------------------------------- #
# Cross-season pair-order consensus
#
# A momentary price inversion (a promo that dips one package under the one below
# it for a day) flips the published ladder in ONE season while the other season
# shows the airline's real hierarchy. Evidence rule: for a brand pair that both
# seasons list, the season whose two fares are FURTHER apart is the one that
# reflects the real ladder — a 2.00 gap is noise, a 55.01 gap is structure.
# --------------------------------------------------------------------------- #
#: ``{frozenset({match_key_a, match_key_b}): (first_key, second_key)}``
PairPrefs = dict


def cross_season_pair_prefs(ladders: dict) -> dict:
    """Derive per-route brand-pair order preferences from multi-season evidence.

    ``ladders`` maps ``(carrier, origin, destination, cabin, season)`` to the
    published ladder as an ordered list of ``(brand_match_key, absolute_price)``.
    For each ``(carrier, origin, destination, cabin)`` seen in ≥2 seasons, every
    brand pair that appears in both seasons **with a different relative order**
    is decided by the season with the LARGER absolute price gap. Equal gaps
    (or a pair whose price is missing in a season) yield no preference.

    Returns ``{(carrier, origin, destination, cabin): {pair: (first, second)}}``.
    """
    by_route: dict = {}
    for (carrier, origin, destination, cabin, season), ladder in ladders.items():
        pos_price: dict[str, tuple[int, Optional[float]]] = {}
        for pos, (key, price) in enumerate(ladder):
            if key and key not in pos_price:          # first occurrence wins
                pos_price[key] = (pos, price)
        by_route.setdefault((carrier, origin, destination, cabin), {})[season] = pos_price

    out: dict = {}
    for route, seasons in by_route.items():
        if len(seasons) < 2:
            continue
        names = sorted(seasons)
        pairs: set[tuple[str, str]] = set()
        for i, s1 in enumerate(names):
            for s2 in names[i + 1:]:
                common = set(seasons[s1]) & set(seasons[s2])
                pairs.update(combinations(sorted(common), 2))
        prefs: dict = {}
        for a, b in sorted(pairs):
            evidence: list[tuple[float, tuple[str, str]]] = []
            for s in names:
                d = seasons[s]
                if a not in d or b not in d:
                    continue
                (pa, va), (pb, vb) = d[a], d[b]
                if va is None or vb is None:
                    continue                          # no price -> no evidence
                evidence.append((abs(va - vb), (a, b) if pa < pb else (b, a)))
            if len(evidence) < 2 or len({o for _, o in evidence}) < 2:
                continue                              # seasons agree -> nothing to repair
            best_gap = max(g for g, _ in evidence)
            winners = {o for g, o in evidence if g == best_gap}
            if len(winners) != 1:
                continue                              # equal gaps disagree -> no preference
            prefs[frozenset((a, b))] = winners.pop()
        if prefs:
            out[route] = prefs
    return out


def _apply_pair_prefs(enriched: list, pair_prefs: dict) -> list:
    """Bubble ADJACENT contradicted pairs into their preferred order.

    Only neighbours are swapped: a non-adjacent contradiction means some third
    package's price sits between the two, and that price ordering wins. Repeats
    until a full pass makes no swap (hard cap: ``len(list) ** 2`` passes).
    """
    keys = [brand_match_key(raw.raw_brand_name) for raw, _nb, _p in enriched]
    n = len(enriched)
    for _ in range(n * n):
        swapped = False
        for i in range(n - 1):
            a, b = keys[i], keys[i + 1]
            if not a or not b or a == b:
                continue
            if pair_prefs.get(frozenset((a, b))) == (b, a):
                enriched[i], enriched[i + 1] = enriched[i + 1], enriched[i]
                keys[i], keys[i + 1] = keys[i + 1], keys[i]
                swapped = True
        if not swapped:
            break
    return enriched


def assign_brand_order(brands: list[RawBrand],
                       pair_prefs: Optional[dict] = None
                       ) -> list[tuple[RawBrand, NormalizedBrand, int]]:
    """Return brands ranked into their true hierarchy for one cabin.

    Primary sort key = **absolute price** — airline sites present a cabin's
    branded ladder cheapest-first, so price is the ground truth for ordering
    (and for the dashboard's per-tier "geçiş" deltas). Name-derived rank and
    screen order only break ties. A leading cabin-base offset keeps cabins from
    interleaving if a mixed list is ever passed. Returns ``(raw, normalized,
    order)`` with ``order`` a 0-based index within the cabin.

    ``pair_prefs`` (from :func:`cross_season_pair_prefs`) then repairs pairs
    whose price order is a momentary inversion, using the other season's
    stronger evidence; orders are renumbered 0..n-1 afterwards.
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
    if pair_prefs:
        enriched = _apply_pair_prefs(enriched, pair_prefs)
    return [(raw, nb, i) for i, (raw, nb, _p) in enumerate(enriched)]


def regroup_brands_by_cabin(brands: list[RawBrand], group_cabin: Cabin,
                            keep_pe: bool = True,
                            carrier: Optional[str] = None) -> dict[Cabin, list[RawBrand]]:
    """Re-bucket brands to their *effective* cabin, dropping contradictions.

    A source can list a fare from another cabin inside a search. The rule is
    **direction-sensitive**:

    * effective cabin == the searched/group cabin -> keep (the normal case);
    * effective cabin ABOVE it (Premium Economy or Business boxes offered inside
      an Economy search) -> a real *upgrade leak*: keep it, bucketed under its
      own cabin. These are genuine sellable fares the site displays in that
      search, and dropping them lost whole cabins (LH ATH-FRA Business);
    * effective cabin BELOW it (an "Economy Light" upsell listed in a Business
      search) -> mislabeled noise, dropped (the $5,877 "business" outlier).

    ``keep_pe`` (kept for signature compatibility) gates the upgrade-leak
    branch: it is set only for the Economy search, so the Business search never
    re-imports higher cabins. Mutates each kept brand's ``cabin`` to the
    effective value.
    """
    out: dict[Cabin, list[RawBrand]] = {}
    for b in brands:
        ov = ff_override(carrier, b.fare_family_code) or ff_override(carrier, b.raw_brand_name)
        if ov:
            b.raw_brand_name = ov[0]     # the OTA's display name for this code is junk
        eff = effective_cabin(b.raw_brand_name, b.fare_family_code, b.cabin,
                              group_cabin, carrier=carrier)
        if eff == group_cabin or (keep_pe and cabin_rank(eff) > cabin_rank(group_cabin)):
            b.cabin = eff
            out.setdefault(eff, []).append(b)
        # else: cabin contradicts the search -> drop as mislabeled noise
    return out


def display_names(raws: list[str], carrier: Optional[str] = None) -> list[str]:
    """Published names for one ladder: the airline's own wording, made unique.

    The canonical sub-tier label is deliberately NOT used here: it collapses
    distinct families (Economy Comfort vs Economy Comfort Green, Business Flex
    vs Business Semi-Flex) into one published row. Where two fares would still
    render identically, the distinguishing words from the raw names are appended
    — never an index, which would invent a difference the airline never showed.
    """
    pretty = [pretty_brand_name(r, carrier) for r in raws]
    counts: dict[str, int] = {}
    for n in pretty:
        counts[n] = counts.get(n, 0) + 1
    out: list[str] = []
    for name, raw in zip(pretty, raws):
        if counts[name] < 2:
            out.append(name)
            continue
        extra = [w for w in re.split(r"[\s/_-]+", (raw or "").strip())
                 if w and w.lower() not in {p.lower() for p in name.split()}]
        out.append(f"{name} {' '.join(extra)}".strip() if extra else name)
    return out


def iter_ranked_by_cabin(brands: list[RawBrand], group_cabin: Cabin,
                         carrier: Optional[str] = None,
                         pair_prefs_by_cabin: Optional[dict] = None):
    """Regroup ``brands`` to their effective cabin, then within each cabin yield
    ``(effective_cabin, raw, normalized, order, absolute_price)`` in price order.

    Single source of truth for every exporter (runner, reprocess, to_platform,
    make_excel) so ordering, tier codes and cabin re-bucketing stay identical.

    ``pair_prefs_by_cabin`` is an optional ``{Cabin: {pair: (first, second)}}``
    map (see :func:`cross_season_pair_prefs`); the effective cabin's slice is
    handed to :func:`assign_brand_order`. NOTE: this function MUTATES the brand
    objects (pretty-cased display name, effective cabin), but idempotently — so
    a pre-pass that builds the preferences from the very same objects is safe.
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
        ranked = assign_brand_order(bs, (pair_prefs_by_cabin or {}).get(eff_cabin))
        abs_prices = compute_absolute_prices([raw for raw, _, _ in ranked])
        shown = display_names([raw.raw_brand_name for raw, _, _ in ranked], carrier)
        # `display_names` only disambiguates when the raw name has an extra
        # word to borrow; two entries with the literal same raw name (a tier
        # pulled in twice — e.g. merge_cross_date_ladder borrowing an
        # identically-named family from another sampled date, live-verified
        # 2026-08-04 on EK where a same-day and a cross-date "Economy Flex"
        # both survived name-based dedup) still come out with an identical
        # published name. One row per name is the invariant the reports
        # promise, so on a collision the cheaper entry wins — same "cheap and
        # plausible over expensive and stray" bias ladder_metrics already
        # applies — and the rest are dropped here, at the single choke point
        # every exporter reads through.
        best_by_name: dict[str, tuple] = {}
        for item in zip(ranked, abs_prices, shown):
            (_raw, _nb, _order), absp, name = item
            cur = best_by_name.get(name)
            if cur is None or (absp or float("inf")) < (cur[1] or float("inf")):
                best_by_name[name] = item
        for (raw, nb, order), absp, name in best_by_name.values():
            # Publish the airline's family name; keep the canonical label for
            # rank/tier so ordering and Eco-N/Bus-N are unchanged.
            nb.canonical_name = nb.normalized_name
            nb.normalized_name = name
            yield eff_cabin, raw, nb, order, absp


def iter_unit_ranked_by_cabin(cabin_results, carrier: Optional[str] = None,
                              pair_prefs_by_cabin: Optional[dict] = None):
    """Publish ONE ladder per effective cabin for a whole scrape unit.

    ``iter_ranked_by_cabin`` works on a single cabin result, so two of them can
    legitimately produce the SAME effective cabin: the Economy result carries a
    Premium-Economy family that leaked into the economy ladder, while a
    dedicated Premium-Economy result also exists (EK AMM-PVG: "Premium Economy
    Flex Plus" 1899.96 inside Economy vs "Premium Economy FlexPlus" 1892.81 in
    the PE result). Concatenating both published near-duplicate rows in one
    (carrier, OND, season, cabin) group, sometimes with descending prices.

    One ladder per effective cabin wins the unit:

    1. the ladder with MORE brands (the richer, more complete ladder);
    2. tie -> the ladder whose cabin result was ALREADY that cabin (the adapter
       picked it deliberately — same principle as ct3 overriding a ct2 leak);
    3. still tied -> the earlier cabin result.

    Yields ``(effective_cabin, raw, normalized, order, absolute_price,
    cabin_result)``; the trailing cabin result is the WINNING ladder's own
    source, so callers read its ``departure`` / ``return_date`` / ``source``
    rather than the one they happen to be looping over.
    """
    ladders: dict[tuple[int, Cabin], list] = {}
    produced: list[tuple[int, Cabin]] = []          # encounter order, for stable output
    for i, c in enumerate(cabin_results):
        if not getattr(c, "brands", None):
            continue
        for eff_cabin, raw, nb, order, absp in iter_ranked_by_cabin(
                c.brands, c.cabin, carrier=carrier,
                pair_prefs_by_cabin=pair_prefs_by_cabin):
            key = (i, eff_cabin)
            if key not in ladders:
                ladders[key] = []
                produced.append(key)
            ladders[key].append((raw, nb, order, absp))

    def strength(key: tuple[int, Cabin]) -> tuple:
        i, eff_cabin = key
        return (len(ladders[key]), int(cabin_results[i].cabin == eff_cabin), -i)

    winner: dict[Cabin, tuple[int, Cabin]] = {}
    for key in produced:
        cur = winner.get(key[1])
        if cur is None or strength(key) > strength(cur):
            winner[key[1]] = key
    for key in produced:
        i, eff_cabin = key
        if winner.get(eff_cabin) != key:
            continue                                # duplicate ladder for this cabin
        for raw, nb, order, absp in ladders[key]:
            yield eff_cabin, raw, nb, order, absp, cabin_results[i]


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


def merge_ladders(primary: list[RawBrand], secondary: list[RawBrand],
                  secondary_source: str = "") -> tuple[list[RawBrand], list[str]]:
    """Top up a ladder with families only the secondary source sells.

    The primary source owns every family it lists — name, price and amenities
    are kept verbatim, because mixing two OTAs' prices for the same fare would
    publish a number no one can reproduce. Families the primary does NOT have
    are appended with their own ``source`` recorded, so the report can say where
    each fare came from. Ordering is left to ``assign_brand_order`` downstream;
    ``screen_order`` is renumbered by price so the raw record stays sensible.

    Returns ``(merged, added_names)``.
    """
    have = {brand_match_key(b.raw_brand_name) for b in primary}
    added: list[RawBrand] = []
    for b in secondary or []:
        key = brand_match_key(b.raw_brand_name)
        if not key or key in have:
            continue                      # primary wins on every shared family
        have.add(key)
        if secondary_source:
            b.source = secondary_source
        added.append(b)
    merged = list(primary) + added
    merged.sort(key=lambda x: (x.price_value if x.price_value is not None else float("inf")))
    for i, b in enumerate(merged):
        b.screen_order = i
    return merged, [b.raw_brand_name for b in added]


def merge_cross_date_ladder(primary: list[RawBrand],
                            others: list[tuple[Optional[date], list[RawBrand]]],
                            adapter_name: str = "") -> tuple[list[RawBrand], list[str]]:
    """Fill tiers missing from ``primary`` using OTHER SAMPLED DATES of the same
    OND/cabin/season/carrier walk — 2026-08-03, opt-in via
    ``Config.merge_cross_date_ladders``.

    ``base.SourceAdapter.run_unit`` already opens every date in the window and
    keeps only the single strongest ladder per cabin, discarding the rest. If
    Tuesday sells [Basic, Smart, Plus] (Go sold out) and Wednesday sells
    [Basic, Smart, Go] (Plus sold out), today's code picks whichever ladder is
    "stronger" and throws the other away whole — even though between the two,
    the OND's real four-tier structure (Basic/Smart/Go/Plus) is fully visible.
    No extra scraping is needed: these dates are walked anyway.

    Unlike ``merge_ladders`` (same-day cross-SOURCE top-up, where mixing
    prices is safe because both sources describe the same day's inventory),
    a borrowed tier here carries a DIFFERENT day's price. Two guards specific
    to that:

    * name matching only (via ``merge_ladders``'s own logic) — never the
      same-length/price-order fallback, which would pair off two genuinely
      different tiers just because both ladders happen to have N entries;
    * a candidate is rejected if adding it raises the ladder's implausible-step
      count (``ladder_metrics``) — a >3x jump is more likely that other day's
      inventory quirk than a real gap in the primary day's structure.

    Returns ``(merged, notes)`` where ``notes`` describes what was imported
    from where, for the same kind of log line ``merge_ladders`` already gets.
    """
    cur = list(primary)
    cur_bad, _ = ladder_metrics(cur)
    notes: list[str] = []
    for dep, other_brands in others:
        if not other_brands:
            continue
        trial, added = merge_ladders(cur, other_brands, adapter_name)
        if not added:
            continue
        bad, _worst = ladder_metrics(trial)
        if bad > cur_bad:
            continue          # would introduce a new implausible jump — skip
        cur = trial
        cur_bad = bad
        when = dep.isoformat() if dep else "?"
        notes.append(f"{', '.join(added)} (from {when})")
    return cur, notes


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
