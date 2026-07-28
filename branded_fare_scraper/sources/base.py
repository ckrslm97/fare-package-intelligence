"""Source adapter interface + registry + shared orchestration template."""

from __future__ import annotations

import abc
import logging
from datetime import date
from typing import Optional

from ..models import Cabin, CabinResult, Job, ScrapeUnit, UnitResult, UnitStatus, now_ts
from ..retry import CarrierAbsent, NoAvailabilityError

_LOG = logging.getLogger("bfs")


class SourceAdapter(abc.ABC):
    """Base class for every source.

    Subclasses implement :meth:`fetch_search` (one route search for a given
    departure/return, returning results for *all* cabins the source exposes).
    The base :meth:`run_unit` handles the 7-day fallback window.
    """

    #: Human-readable source label written into the output ``Source`` column.
    name: str = "unknown"
    #: Lower number == higher priority. Official sites are 1; OTAs 2+.
    priority: int = 100
    #: Whether this adapter needs a real browser page.
    needs_browser: bool = True

    def supports(self, carrier: str) -> bool:
        """Whether this adapter can handle the given carrier. Override me."""
        return False

    def cabins_for(self, job: Job) -> list[Cabin]:
        """Cabins we intend to collect. Sources that return all cabins in one
        search can ignore this; it exists for validation / DOM-tab adapters."""
        return [Cabin.ECONOMY, Cabin.PREMIUM_ECONOMY, Cabin.BUSINESS]

    @abc.abstractmethod
    async def fetch_search(
        self, page, job: Job, departure: date, return_date: date
    ) -> list[CabinResult]:
        """Perform one search; return a CabinResult per cabin found.

        Raise :class:`NoAvailabilityError` if the date has no flights at all
        (the base will then try the next date in the window).
        """
        raise NotImplementedError

    async def run_unit(self, page, unit: ScrapeUnit) -> UnitResult:
        """Try each (departure, return) in the fallback window; first with data wins."""
        result = UnitResult(unit=unit, source=self.name, started_at=now_ts())
        last_error = ""
        # Opportunistic carriers (PC/VF) only try the primary date — no point
        # walking the whole fallback window for a carrier that isn't on the route.
        window = unit.date_plan.window[:1] if unit.opportunistic else unit.date_plan.window
        # A carrier that flies < daily (common long-haul) legitimately has no seat
        # on some window dates, so "flights exist but not this carrier" is only
        # treated as terminal after it repeats — that's what the window is for.
        absent = 0
        absent_limit = 1 if unit.opportunistic else 3
        for dep, ret in window:
            try:
                cabins = await self.fetch_search(page, unit.job, dep, ret)
            except CarrierAbsent as e:
                last_error = str(e)
                absent += 1
                if absent >= absent_limit:
                    break
                continue
            except NoAvailabilityError as e:
                last_error = str(e)
                _LOG.debug("%s %s %s: no availability on %s", self.name,
                           unit.job.route, unit.date_plan.season.value, dep)
                continue
            if cabins and any(c.brands for c in cabins):
                result.cabin_results = cabins
                result.status = UnitStatus.SUCCESS
                result.finished_at = now_ts()
                return result
            last_error = "empty result"
        # Nothing in the whole window.
        result.status = UnitStatus.NO_AVAILABILITY
        result.error = last_error or "no availability in date window"
        result.finished_at = now_ts()
        return result


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #
_REGISTRY: list[SourceAdapter] = []


def register(cls):
    """Class decorator: instantiate and register a source adapter."""
    instance = cls()
    _REGISTRY.append(instance)
    return cls


def registered_sources() -> list[SourceAdapter]:
    return sorted(_REGISTRY, key=lambda s: s.priority)


def reset_caches() -> None:
    """Clear adapters' process-global dict caches/locks (call at run start) so a
    second run in the same process can't reuse stale results or dead-loop locks."""
    for s in _REGISTRY:
        for attr in ("_cache", "_result_cache", "_slug_cache", "_locks", "_result_locks"):
            v = getattr(type(s), attr, None)
            if isinstance(v, dict):
                v.clear()


def get_sources_for(carrier: str, only_names: Optional[list[str]] = None) -> list[SourceAdapter]:
    """Adapters that can serve ``carrier``, ordered by priority (fallback chain)."""
    out = []
    for s in registered_sources():
        if only_names and s.name not in only_names:
            continue
        if s.supports(carrier):
            out.append(s)
    return out
