"""Core data model for the branded-fare scraper.

Two layers are modelled deliberately:

* **Raw layer** (``RawBrand``): what a source adapter extracts from a page,
  as close to the screen as possible. Prices may be absolute *or* deltas here.
* **Normalized layer** (``BrandedFare``): the standard, flat record that is
  written to the output. One ``BrandedFare`` == one row in the normalized file.

Nothing here maps fare families — that is a downstream concern.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any, Optional


# --------------------------------------------------------------------------- #
# Enums
# --------------------------------------------------------------------------- #
class Season(str, Enum):
    SUMMER = "Summer"
    WINTER = "Winter"


class Cabin(str, Enum):
    ECONOMY = "Economy"
    PREMIUM_ECONOMY = "Premium Economy"
    BUSINESS = "Business"
    FIRST = "First"


class AmenityStatus(str, Enum):
    """Status of a single amenity/right inside a branded fare.

    Rule from the spec: if a right is *available for a fee* it is ``PAID`` —
    never ``NOT_INCLUDED``. ``NOT_INCLUDED`` means genuinely unavailable.
    ``UNKNOWN`` means the page did not expose the information.
    """

    INCLUDED = "Included"
    PAID = "Paid"
    NOT_INCLUDED = "Not Included"
    UNKNOWN = "Unknown"


class PriceType(str, Enum):
    ABSOLUTE = "absolute"   # e.g. "230 USD"
    DELTA = "delta"         # e.g. "+30 USD" relative to the cabin's base fare


class UnitStatus(str, Enum):
    PENDING = "pending"
    SUCCESS = "success"
    PARTIAL = "partial"      # some data, but validation flagged gaps
    NO_AVAILABILITY = "no_availability"
    FAILED = "failed"


# --------------------------------------------------------------------------- #
# Input
# --------------------------------------------------------------------------- #
@dataclass
class Job:
    """One row of the input Excel — an independent scraping task."""

    carrier: str
    origin: str
    destination: str
    raw_row: dict[str, Any] = field(default_factory=dict)
    job_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    @property
    def route(self) -> str:
        return f"{self.origin}-{self.destination}"

    def key(self) -> str:
        return f"{self.carrier}|{self.origin}|{self.destination}".upper()


@dataclass
class DatePlan:
    """A departure/return plan for one season, with a fallback window.

    ``window`` is the ordered list of ``(departure, return)`` candidates to try
    until an adapter finds availability. ``return`` is always ``departure + 3``.
    """

    season: Season
    departure: date
    return_date: date
    window: list[tuple[date, date]]
    #: Candidates are walked in BLOCKS of this size (far3: 3 consecutive days
    #: per lead-time band). 0 = the whole window is one block (legacy walk).
    #: A later block is only tried when the earlier one produced nothing.
    block_size: int = 0

    @property
    def blocks(self) -> list[list[tuple[date, date]]]:
        """The window split into its ordered fallback blocks."""
        n = self.block_size or len(self.window) or 1
        return [self.window[i:i + n] for i in range(0, len(self.window), n)] or [[]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "season": self.season.value,
            "departure": self.departure.isoformat(),
            "return_date": self.return_date.isoformat(),
            "window": [[d.isoformat(), r.isoformat()] for d, r in self.window],
            "block_size": self.block_size,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "DatePlan":
        return cls(
            season=Season(d["season"]),
            departure=date.fromisoformat(d["departure"]),
            return_date=date.fromisoformat(d["return_date"]),
            window=[(date.fromisoformat(a), date.fromisoformat(b)) for a, b in d["window"]],
            block_size=int(d.get("block_size", 0)),      # older plans: one block
        )


# --------------------------------------------------------------------------- #
# Raw scrape layer
# --------------------------------------------------------------------------- #
@dataclass
class RawAmenity:
    """A single right/amenity as read from the page, pre-normalization."""

    raw_label: str
    status: AmenityStatus
    raw_value: str = ""          # e.g. "23 kg", "+25 EUR", "included"
    fee_amount: Optional[float] = None
    fee_currency: Optional[str] = None
    canonical_key: Optional[str] = None   # resolved canonical amenity key, if known


@dataclass
class RawMiles:
    mileage_available: Optional[bool] = None
    miles_earned: Optional[float] = None
    bonus_percent: Optional[float] = None
    raw_text: str = ""


@dataclass
class RawBrand:
    """A branded fare exactly as extracted from a page (pre-normalization)."""

    raw_brand_name: str
    cabin: Cabin
    screen_order: int                      # position on screen (0-based)
    price_value: Optional[float] = None
    price_type: PriceType = PriceType.ABSOLUTE
    currency: Optional[str] = None
    display_price_text: str = ""           # verbatim price string from the page
    fare_family_code: Optional[str] = None
    description: str = ""
    amenities: list[RawAmenity] = field(default_factory=list)
    miles: RawMiles = field(default_factory=RawMiles)
    #: Which adapter contributed THIS fare. Normally the cabin's own source;
    #: set explicitly when a top-up source adds a family the primary missed.
    source: str = ""


@dataclass
class CabinResult:
    """Result of scraping a single cabin within a unit."""

    cabin: Cabin
    departure: Optional[date] = None       # date actually used (from the window)
    return_date: Optional[date] = None
    brands: list[RawBrand] = field(default_factory=list)
    has_availability: bool = True
    note: str = ""
    source: str = ""                       # which adapter provided this cabin

    # Identity of the itinerary this ladder was actually read from. Trip.com's
    # raw never carried it, which is why the 2026-08-02 name-authority work had
    # no join key and was abandoned: two sources could describe the same flight
    # and nothing tied them together. Enuygun publishes all of it, so a
    # cross-source check on ONE flight becomes possible instead of a comparison
    # of two ladders that may not even be the same departure.
    flight_no: str = ""                    # e.g. "GQ700" (marketing)
    booking_class: str = ""                # RBD, e.g. "D" / "F"
    operating_carrier: str = ""            # IATA of the metal's operator
    #: The user's rules treat these differently: an interline itinerary is
    #: dropped (it sells another carrier's product), a codeshare is KEPT but
    #: must be visible. Sources that state them explicitly beat inferring from
    #: an operator-name string comparison.
    is_codeshare: Optional[bool] = None
    is_interline: Optional[bool] = None


# --------------------------------------------------------------------------- #
# Normalized output layer
# --------------------------------------------------------------------------- #
@dataclass
class BrandedFare:
    """One normalized output row (flattened to columns by ``io_utils``)."""

    carrier: str
    origin: str
    destination: str
    departure_date: Optional[date]
    return_date: Optional[date]
    season: Season
    cabin: Cabin
    brand_order: int                       # 0-based order within the cabin
    tier_code: str                         # Eco-1 / Eco-2 / PEco-1 / Bus-1 ...
    raw_brand_name: str
    normalized_brand_name: str
    display_price: str
    calculated_absolute_price: Optional[float]
    currency: Optional[str]
    source: str
    fare_family_code: Optional[str]
    brand_description: str
    # amenity key -> AmenityStatus.value
    amenities: dict[str, str] = field(default_factory=dict)
    # amenity key -> human detail (e.g. "23 KG", "3307 TRY") for tooltips
    amenity_details: dict[str, str] = field(default_factory=dict)
    mileage_available: Optional[bool] = None
    miles_earned: Optional[float] = None
    bonus_percent: Optional[float] = None
    scrape_timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
    status: str = UnitStatus.SUCCESS.value
    retry_count: int = 0


# --------------------------------------------------------------------------- #
# Unit orchestration
# --------------------------------------------------------------------------- #
@dataclass
class ScrapeUnit:
    """The atomic parallel work item: one job for one season.

    A unit iterates every cabin (and the source-priority fallback chain)
    internally so that one browser session performs one route search.
    """

    job: Job
    date_plan: DatePlan
    # Opportunistic units (e.g. PC/VF added to every OND) only yield output where
    # the carrier actually operates; "no availability" for them is not a failure.
    opportunistic: bool = False

    def key(self) -> str:
        return f"{self.job.key()}|{self.date_plan.season.value}".upper()


@dataclass
class UnitResult:
    unit: ScrapeUnit
    source: Optional[str] = None
    cabin_results: list[CabinResult] = field(default_factory=list)
    status: UnitStatus = UnitStatus.PENDING
    retry_count: int = 0
    error: str = ""
    started_at: float = 0.0
    finished_at: float = 0.0
    validation_issues: list[str] = field(default_factory=list)
    sources_tried: list[str] = field(default_factory=list)

    @property
    def elapsed(self) -> float:
        if self.started_at and self.finished_at:
            return round(self.finished_at - self.started_at, 3)
        return 0.0

    def total_brands(self) -> int:
        return sum(len(c.brands) for c in self.cabin_results)


@dataclass
class RunSummary:
    run_id: str
    started_at: str
    finished_at: str = ""
    total_units: int = 0
    total_jobs: int = 0
    succeeded: int = 0
    partial: int = 0
    failed: int = 0
    no_availability: int = 0
    skipped_checkpoint: int = 0
    opportunistic_absent: int = 0
    units_missing_cabin: int = 0
    units_missing_brands: int = 0
    units_missing_price: int = 0
    units_missing_amenities: int = 0
    units_missing_miles: int = 0
    units_missing_source: int = 0
    units_missing_business_cabin: int = 0
    total_normalized_rows: int = 0
    total_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items()}


def now_ts() -> float:
    return time.monotonic()
