"""Canonical amenity (right) taxonomy + status classification.

The taxonomy below is fixed to the reference comparison the user provided — the
same rows, in the same order (El Bagajı … Pet), plus miles handled separately.
Each canonical right becomes one output column and one row in the visual report.

Status rule (from the spec): a right that is *purchasable for a fee* is ``PAID``
— never ``NOT_INCLUDED``. ``NOT_INCLUDED`` means genuinely unavailable in the
fare. ``UNKNOWN`` means the source did not expose the information.
"""

from __future__ import annotations

import re
from typing import Optional

from .models import AmenityStatus

# --------------------------------------------------------------------------- #
# Canonical amenities — (key, English display, Turkish label). Order == output
# column order == visual report row order (matches the reference image).
# --------------------------------------------------------------------------- #
CANONICAL_AMENITIES: list[tuple[str, str, str]] = [
    ("cabin_baggage", "Cabin Baggage", "El Bagajı"),
    ("checked_baggage", "Checked Baggage", "Check-in Bagajı"),
    ("seat_selection", "Seat Selection", "Koltuk Seçimi"),
    ("meal", "Meal", "Yemek"),
    ("lounge_access", "Lounge", "Lounge"),
    ("priority_boarding", "Priority Boarding", "Priority Boarding"),
    ("fast_track", "Fast Track", "Fast Track"),
    ("refund", "Refund", "Refund"),
    ("change", "Change", "Change"),
    ("no_show_refund", "No-Show Refund", "No-Show Refund"),
    ("no_show_change", "No-Show Change", "No-Show Change"),
    ("same_day_earlier_flight", "Same Day Earlier Flight", "Aynı Gün Erken Uçuş"),
    ("wifi", "WiFi", "WiFi"),
    ("extra_baggage", "Extra Baggage", "Extra Baggage"),
    ("sports_equipment", "Sports Equipment", "Spor Ekipmanı"),
    ("pet", "Pet", "Pet"),
]

AMENITY_KEYS: list[str] = [k for k, _, _ in CANONICAL_AMENITIES]
AMENITY_DISPLAY: dict[str, str] = {k: en for k, en, _ in CANONICAL_AMENITIES}
AMENITY_TR: dict[str, str] = {k: tr for k, _, tr in CANONICAL_AMENITIES}

# Free-text / rule-token synonyms (EN + TR + OTA rule types), matched as
# lowercase substrings. Adapters with structured rule types set the canonical
# key directly; this map is the fallback (Enuygun DOM text, etc.).
SYNONYMS: dict[str, list[str]] = {
    "cabin_baggage": ["cabin bag", "carry-on", "carry on", "hand bag", "hand lug",
                      "el bagaj", "kabin bagaj", "cabinbaggage"],
    "checked_baggage": ["checked bag", "hold bag", "baggage allowance", "free baggage",
                        "check-in bag", "checkin bag", "bagaj hakk", "kayıtlı bagaj",
                        "baggage"],
    "seat_selection": ["seat selection", "seat choice", "choose seat", "preferred seat",
                       "standard seat", "koltuk seç", "koltuk", "seatselection"],
    "meal": ["meal", "catering", "buy on board", "food", "ikram", "yemek"],
    "lounge_access": ["lounge", "salon", "cip lounge", "vip lounge", "loungeaccess"],
    "priority_boarding": ["priority boarding", "prior board", "öncelikli biniş",
                          "priorityboarding"],
    "fast_track": ["fast track", "fast-track", "hızlı geçiş", "priority security",
                   "priority_security", "prioritysecurity"],
    "refund": ["refund", "iade", "para iade", "geri ödeme", "cancel_before",
               "cancellation before", "cancellation"],
    "change": ["change after", "reissue", "rebook", "date change", "biletdeğişik",
               "tarih değişik", "change_afterdeparture", "change after departure",
               "değişiklik", "exchange"],
    "no_show_refund": ["no-show refund", "no show refund", "cancel_noshow", "noshow refund",
                       "gelmeme iade", "refund with penalty for no-show",
                       "penalty for no-show", "no-show cancellation"],
    "no_show_change": ["no-show change", "no show change", "change_noshow", "noshow change",
                       "gelmeme değişik", "change with no-show"],
    "same_day_earlier_flight": ["same day", "same-day", "earlier flight", "aynı gün",
                                "standby earlier", "earlier same day"],
    "wifi": ["wifi", "wi-fi", "wireless internet", "kablosuz internet",
             "internet package", "internet paketi"],
    "extra_baggage": ["extra bag", "additional bag", "excess bag", "ek bagaj", "fazla bagaj",
                      "extrabaggage"],
    "sports_equipment": ["sports equipment", "sport equip", "spor ekipman", "sportsequipment"],
    "pet": ["pet", "evcil hayvan", "pet in cabin", "avih"],
}

# Signals for free-text status detection. Order of application matters:
# fee -> excluded -> included (so "not permitted"/"not included" never read as
# "permitted"/"included").
_PAID_HINTS = ["fee", "paid", "chargeable", "for a fee", "with charge", "deduction",
               "penalty", "purchas", "at charge", "at cost", "extra charge",
               "ücretli", "ek ücret"]
# A "+" only signals a fee when it precedes a number (e.g. "+25 EUR"), not "2+ days".
_PLUS_MONEY = re.compile(r"\+\s*\d")
_EXCLUDED_HINTS = ["not included", "not allowed", "not permitted", "unavailable",
                   "not available", "✗", "✕", "—", "dahil değil", "hariç", "yok",
                   "0 x 0",
                   # Ubfly rule phrasings ("Non-refundable.", "Non-exchangeable.")
                   "non-refundable", "non refundable", "nonrefundable", "not refundable",
                   "non-exchangeable", "non exchangeable", "not exchangeable",
                   "non-changeable", "iade edilemez", "iadesiz", "değiştirilemez"]
_INCLUDED_HINTS = ["included", "free of charge", "free", "allowed", "permitted",
                   "complimentary", "accrual", "✓", "✔", "dahil", "ücretsiz", "var"]
_EXCLUDED_WORDS = re.compile(r"\b(no|none)\b", re.IGNORECASE)
_INCLUDED_WORDS = re.compile(r"\b(yes|var)\b", re.IGNORECASE)

_MONEY_RE = re.compile(r"[\$€₺£]|\b\d+[.,]?\d*\s?(usd|eur|try|gbp|tl)\b", re.IGNORECASE)


def normalize_text(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def map_label_to_canonical(label: str) -> Optional[str]:
    """Best-effort map of a free-text amenity label onto a canonical key."""
    t = normalize_text(label)
    if not t:
        return None
    best: Optional[str] = None
    best_len = 0
    for key, subs in SYNONYMS.items():
        for sub in subs:
            if sub in t and len(sub) > best_len:
                best, best_len = key, len(sub)
    return best


def classify_status_from_text(value_text: str, has_money: Optional[bool] = None) -> AmenityStatus:
    """Classify a right's status from a rendered value/state string.

    Precedence: fee/price -> Paid; explicit negatives -> Not Included; positives
    -> Included; otherwise Unknown. Negatives are checked before positives so
    ``"not permitted"`` is never misread as ``"permitted"``.
    """
    t = normalize_text(value_text)
    money = has_money if has_money is not None else bool(_MONEY_RE.search(value_text or ""))
    if money or any(h in t for h in _PAID_HINTS) or _PLUS_MONEY.search(value_text or ""):
        return AmenityStatus.PAID
    if any(h in t for h in _EXCLUDED_HINTS) or _EXCLUDED_WORDS.search(t):
        return AmenityStatus.NOT_INCLUDED
    if any(h in t for h in _INCLUDED_HINTS) or _INCLUDED_WORDS.search(t):
        return AmenityStatus.INCLUDED
    return AmenityStatus.UNKNOWN


def empty_amenity_map() -> dict[str, str]:
    """A full amenity map defaulted to UNKNOWN (so every column is always present)."""
    return {k: AmenityStatus.UNKNOWN.value for k in AMENITY_KEYS}


# Canonical Turkish display terms for the rule-type rights. Sources phrase the
# same fact many ways ("Paid", "Ücretli", "at charge", "Cezasız", "Free",
# "Refundable", "Non-exchangeable" …) — exports standardize on one vocabulary,
# keyed by (right, status). Not Included needs no word: the — state carries it.
_RULE_DETAIL_TR: dict[str, dict[AmenityStatus, str]] = {
    "refund": {AmenityStatus.PAID: "Kesintili", AmenityStatus.INCLUDED: "Kesintisiz"},
    "change": {AmenityStatus.PAID: "Ücretli", AmenityStatus.INCLUDED: "Cezasız"},
    "no_show_refund": {AmenityStatus.PAID: "Kesintili", AmenityStatus.INCLUDED: "Kesintisiz"},
    "no_show_change": {AmenityStatus.PAID: "Ücretli", AmenityStatus.INCLUDED: "Cezasız"},
    "same_day_earlier_flight": {AmenityStatus.PAID: "Ücretli", AmenityStatus.INCLUDED: "Ücretsiz"},
}


def canonical_rule_detail(key: str, status: AmenityStatus) -> Optional[str]:
    """Standard display detail for a rule right, or None if ``key`` isn't one."""
    m = _RULE_DETAIL_TR.get(key)
    if m is None:
        return None
    return m.get(status, "")
