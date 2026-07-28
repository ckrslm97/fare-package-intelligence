"""Display price + calculated absolute price.

Some sources show absolute prices for every package; others anchor on a base
fare and show deltas (``+30``) for the rest. This module always produces an
absolute price, keeping the original display string untouched.

Rule (from the spec example): deltas are measured from the cabin's base
(anchor) fare, not from the immediately-preceding package:

    Basic 200            -> 200
    Classic +30          -> 230   (200 + 30)
    Flex   +60           -> 260   (200 + 60)

So the anchor is the most recent *absolute* price seen while walking the
packages in hierarchy order; deltas add onto that anchor.
"""

from __future__ import annotations

import re
from typing import Optional

from .models import PriceType, RawBrand

_NUM_RE = re.compile(r"[-+]?\d[\d.,]*")
_CURRENCY_SIGNS = {"$": "USD", "€": "EUR", "₺": "TRY", "£": "GBP", "TL": "TRY"}


def parse_price(text: str) -> tuple[Optional[float], PriceType, Optional[str]]:
    """Parse a rendered price string into (value, type, currency).

    Handles ``"+30"`` / ``"+ 30 USD"`` (delta) vs ``"230 USD"`` / ``"₺16.306,99"``
    (absolute), and both ``1,234.56`` and ``1.234,56`` decimal conventions.
    """
    if not text:
        return None, PriceType.ABSOLUTE, None
    raw = text.strip()
    is_delta = raw.lstrip().startswith("+")

    currency: Optional[str] = None
    for sign, code in _CURRENCY_SIGNS.items():
        if sign in raw:
            currency = code
            break
    m = re.search(r"\b(USD|EUR|TRY|GBP|TL)\b", raw, re.IGNORECASE)
    if m and not currency:
        currency = _CURRENCY_SIGNS.get(m.group(1).upper(), m.group(1).upper())

    num_match = _NUM_RE.search(raw)
    if not num_match:
        return None, PriceType.DELTA if is_delta else PriceType.ABSOLUTE, currency
    num = num_match.group(0).lstrip("+")

    # Decide decimal separator by which of '.'/',' appears last.
    if "," in num and "." in num:
        if num.rfind(",") > num.rfind("."):      # 1.234,56  (EU)
            num = num.replace(".", "").replace(",", ".")
        else:                                     # 1,234.56  (US)
            num = num.replace(",", "")
    elif "," in num:
        # Ambiguous: treat comma as decimal only when it looks like cents.
        if re.search(r",\d{1,2}$", num):
            num = num.replace(".", "").replace(",", ".")
        else:
            num = num.replace(",", "")
    try:
        value = float(num)
    except ValueError:
        return None, PriceType.DELTA if is_delta else PriceType.ABSOLUTE, currency

    return value, (PriceType.DELTA if is_delta else PriceType.ABSOLUTE), currency


def compute_absolute_prices(ranked_brands: list[RawBrand]) -> list[Optional[float]]:
    """Given brands already in hierarchy order, return each absolute price.

    ``ranked_brands`` MUST be ordered by the true hierarchy (see
    ``normalization.assign_brand_order``) so the anchor logic is meaningful.
    """
    anchor: Optional[float] = None
    out: list[Optional[float]] = []
    for b in ranked_brands:
        if b.price_type == PriceType.ABSOLUTE and b.price_value is not None:
            anchor = b.price_value
            out.append(b.price_value)
        elif b.price_type == PriceType.DELTA and b.price_value is not None:
            out.append(round(anchor + b.price_value, 2) if anchor is not None else None)
        else:
            out.append(None)
    return out
