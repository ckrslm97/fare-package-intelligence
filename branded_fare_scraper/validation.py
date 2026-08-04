"""Post-scrape completeness validation.

After a unit is scraped we check for the gaps the spec calls out (missing cabin,
empty packages, missing price, empty rights, missing miles, missing source).
The runner uses ``needs_retry`` to decide whether to re-queue a unit, and the
issue flags feed the Summary counters.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import AmenityStatus, Cabin, UnitResult, UnitStatus
from .normalization import LCC_CARRIERS


@dataclass
class ValidationReport:
    missing_cabin: bool = False
    missing_brands: bool = False
    missing_price: bool = False
    missing_amenities: bool = False
    missing_miles: bool = False
    missing_source: bool = False
    missing_business_cabin: bool = False
    issues: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.issues

    def needs_retry(self) -> bool:
        # Empty results / missing source / missing prices are worth another try,
        # and so is a Business gap on a carrier that should have one (see
        # missing_business_cabin below) — the runner's single bounded
        # validation-retry pass re-attempts exactly these units.
        return (self.missing_brands or self.missing_source or self.missing_price
                or self.missing_business_cabin)


def validate_unit(result: UnitResult, expected_min_cabins: int = 1,
                  carrier: str = "") -> ValidationReport:
    r = ValidationReport()

    if not result.source:
        r.missing_source = True
        r.issues.append("no source recorded")

    non_empty_cabins = [c for c in result.cabin_results if c.brands]
    if len(result.cabin_results) < expected_min_cabins:
        r.missing_cabin = True
        r.issues.append(f"cabins found={len(result.cabin_results)} < expected {expected_min_cabins}")

    # A full-service carrier (anything outside LCC_CARRIERS) that came back
    # with Economy but no Business at all is exactly the shape a silent
    # search failure takes — the dedicated `sinif=business` request can come
    # back empty from a timeout or a thin response on the SAME unit where the
    # Economy request succeeded (see enuygun.py _do_search: a poll timeout
    # and a genuine "no business flights" both fall through to `return []`,
    # indistinguishable from here). One bounded extra attempt (the runner's
    # validation-retry pass) tells the two apart; flagging it unconditionally
    # for every LCC would instead retry a legitimate, permanent absence on
    # every run for nothing.
    if (carrier and carrier.upper() not in LCC_CARRIERS
            and not any(c.cabin == Cabin.BUSINESS for c in non_empty_cabins)):
        r.missing_business_cabin = True
        r.issues.append(f"no Business cabin for non-LCC carrier {carrier.upper()}")

    total_brands = sum(len(c.brands) for c in result.cabin_results)
    if total_brands == 0:
        r.missing_brands = True
        r.issues.append("no branded fares extracted")

    # Price / amenity / miles checks over the brands we did get.
    priced = 0
    with_amenities = 0
    with_miles = 0
    for c in non_empty_cabins:
        for b in c.brands:
            if b.price_value is not None:
                priced += 1
            if any(a.status != AmenityStatus.UNKNOWN for a in b.amenities):
                with_amenities += 1
            if b.miles.mileage_available is not None or b.miles.miles_earned is not None:
                with_miles += 1

    if total_brands and priced == 0:
        r.missing_price = True
        r.issues.append("no prices on any brand")
    if total_brands and with_amenities == 0:
        r.missing_amenities = True
        r.issues.append("no amenity data on any brand")
    if total_brands and with_miles == 0:
        r.missing_miles = True
        r.issues.append("no miles data on any brand")

    return r


def apply_status(result: UnitResult, report: ValidationReport) -> None:
    """Set the unit's final status from its result + validation."""
    if report.missing_brands:
        if result.status not in (UnitStatus.FAILED, UnitStatus.NO_AVAILABILITY):
            result.status = UnitStatus.NO_AVAILABILITY if not result.error else UnitStatus.FAILED
    elif report.issues:
        result.status = UnitStatus.PARTIAL
    else:
        result.status = UnitStatus.SUCCESS
