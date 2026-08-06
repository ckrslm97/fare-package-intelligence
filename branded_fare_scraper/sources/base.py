"""Source adapter interface + registry + shared orchestration template."""

from __future__ import annotations

import abc
import logging
import re
from datetime import date
from typing import Optional

from ..models import Cabin, CabinResult, Job, ScrapeUnit, UnitResult, UnitStatus, now_ts
from ..report import CARRIER_ALIASES, CARRIER_NAMES
from ..retry import CarrierAbsent, NoAvailabilityError

_LOG = logging.getLogger("bfs")


# --------------------------------------------------------------------------- #
# Airline identity (shared by every adapter that reads row text)
# --------------------------------------------------------------------------- #
def norm_airline_name(name: Optional[str]) -> str:
    """Lowercased, punctuation-free airline name for fuzzy comparison."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", (name or "").lower())).strip()


def resolve_airline_code(name: Optional[str]) -> Optional[str]:
    """IATA code for an airline display name, only when unambiguous.

    Exact match on the project's carrier-name table first, then a *unique*
    containment match so "Aegean Airlines" resolves to A3 ("Aegean"). Ambiguous
    names ("China ..." matches several) resolve to None rather than guessing.

    Aliases carry the spellings an OTA prints that the airline itself does not
    ("All Nippon Airways" for ANA, "Scandinavian Airlines" for SAS). They exist
    because a name this function cannot resolve is a row silently dropped: the
    unit then reports "carrier not on route" while the page is plainly listing
    it. Aliases only MATCH — the published name is still CARRIER_NAMES'.
    """
    n = norm_airline_name(name)
    if not n:
        return None

    def names(code: str):
        yield CARRIER_NAMES.get(code) or ""
        yield from CARRIER_ALIASES.get(code, ())

    exact = [c for c in CARRIER_NAMES
             if any(norm_airline_name(x) == n for x in names(c))]
    if exact:
        return exact[0]
    hits = {c for c in CARRIER_NAMES
            if any((d := norm_airline_name(x)) and (d in n or n in d)
                   for x in names(c))}
    return hits.pop() if len(hits) == 1 else None


def operator_matches(operating: Optional[str], target: str) -> bool:
    """False when a row is operated by a carrier OTHER than ``target``.

    User rule: if another carrier operates the flight it is interline and must
    be dropped — an LH-marketed, Aegean-operated row sells Aegean's product.
    No operating info -> nothing contradicts, keep the row. When the operator
    name resolves to an IATA code, that code decides; otherwise fall back to
    containment against the target's own display name. If neither is known we
    cannot judge and keep the row rather than silently losing the carrier.
    """
    op = (operating or "").strip()
    if not op:
        return True
    code = resolve_airline_code(op)
    if code is not None:
        return code == target.upper()
    disp = norm_airline_name(CARRIER_NAMES.get(target.upper()))
    if not disp:
        _LOG.debug("unresolved operator %r for %s — keeping row", op, target)
        return True
    return disp in norm_airline_name(op)


def row_is_target_carrier(text: str, target: str) -> bool:
    """Whether a results-row's text identifies ``target`` (name, else code)."""
    disp = CARRIER_NAMES.get(target.upper())
    if disp and norm_airline_name(disp) in norm_airline_name(text):
        return True
    return bool(re.search(rf"\b{re.escape(target.upper())}\s?-?\s?\d{{1,4}}\b",
                          (text or "").upper()))


def extraction_signature(flights) -> list[tuple]:
    """Comparable fingerprint of one DOM extraction payload.

    ``(carrier, base price text, {brand name + price text})`` per flight, order
    independent, so two reads of the same rendered page compare equal even if
    the site re-sorts rows between reads.
    """
    out: list[tuple] = []
    for f in flights or []:
        brands = sorted((str(b.get("name") or "").strip(),
                         str(b.get("priceText") or "").strip())
                        for b in (f.get("brands") or []))
        out.append(((f.get("carrier") or "").upper(),
                    (f.get("baseText") or "").strip(), tuple(brands)))
    return sorted(out)


def extraction_diff(first, second) -> list[str]:
    """Differences between two extractions of the SAME loaded page.

    The user's rule is to cross-check what we wrote against what the screen
    shows ("yazdığın ile ekranda yazanı cross check"): parsing twice catches a
    payload read while the results table was still re-rendering. Empty list =
    the two reads agree. Shared so every browser adapter verifies identically.
    """
    a, b = extraction_signature(first), extraction_signature(second)
    if a == b:
        return []
    only_a = [x for x in a if x not in b]
    only_b = [x for x in b if x not in a]
    diff = []
    if len(a) != len(b):
        diff.append(f"flight count {len(a)} -> {len(b)}")
    for car, base, brands in only_a[:3]:
        diff.append(f"1st only: {car} base={base!r} brands={len(brands)}")
    for car, base, brands in only_b[:3]:
        diff.append(f"2nd only: {car} base={base!r} brands={len(brands)}")
    return diff or [f"reordered/equal-size mismatch ({len(a)} flights)"]


def ladder_content(cabin_result: CabinResult) -> int:
    """Content lines in a ladder: parsed amenity/rule entries + free-text rules.

    The user's rule for picking between equally long ladders is "en çok paket
    olan uçuşu ve **en çok içeriği olan paketi** al" — so when two dates sell
    the same number of packages, the one whose packages actually list more
    rights/rules wins.
    """
    total = 0
    for b in cabin_result.brands:
        total += len(b.amenities)
        desc = (b.description or "").strip()
        if desc:
            total += len([p for p in desc.split(";") if p.strip()])
    return total


def ladder_strength(cabin_result: CabinResult) -> tuple[int, int]:
    """Comparable quality of one cabin's ladder: (brand count, content lines)."""
    return (len(cabin_result.brands), ladder_content(cabin_result))


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
    #: Opt-in (2026-08-03 pilot, off by default): fill a cabin's missing tiers
    #: from OTHER dates already walked in this same window instead of keeping
    #: only the single strongest date and discarding the rest. Set from
    #: Config.merge_cross_date_ladders at run start (see Runner.run). Shared
    #: here because run_unit — the date-window walk — lives on this base
    #: class, not on any one adapter.
    merge_cross_date: bool = False

    def supports(self, carrier: str) -> bool:
        """Whether this adapter can handle the given carrier. Override me."""
        return False

    def cabins_for(self, job: Job) -> list[Cabin]:
        """Cabins we intend to collect. Sources that return all cabins in one
        search can ignore this; it exists for validation / DOM-tab adapters."""
        return [Cabin.ECONOMY, Cabin.PREMIUM_ECONOMY, Cabin.BUSINESS]

    @classmethod
    def reset_state(cls) -> None:
        """Clear non-cache per-run state (probe results, feature flags).

        ``reset_caches`` empties the dict/set caches; anything scalar an adapter
        remembers for a whole run (e.g. "premium-economy search is unusable")
        must be cleared here so a second run in the same process re-learns it.
        """
        return None

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
        """Walk the WHOLE (departure, return) window and keep the best ladders.

        User rule: look at every flight across the departure date and the 14
        days after it, then publish "en çok paket olan uçuşu ve en çok içeriği
        olan paketi" — so there is no early exit once the wanted cabins are
        filled; every date in the period is inspected and each cabin keeps its
        single best ``CabinResult``:

        1. more brands (a fuller ladder — EK ISB-MAN's 3-fare business ladder
           beats the 2-fare one that the first date happened to sell);
        2. tie -> more content lines (amenity/rule entries plus free-text rule
           lines summed over the ladder), i.e. the richer packages;
        3. still tied -> the earlier date's capture, so results stay stable
           while prices move during the day.

        A date where only one cabin sells (business found, economy sold out /
        carrier not listed in that day's economy results) never ends the search
        for the other cabins — each ``CabinResult`` carries its own departure
        date, so cabins found on different window days (and replacements found
        later) merge cleanly.

        Cost: walking all 15 dates is bounded by the adapters' per-(OND, date,
        cabin) search caches — one OND/date search serves every carrier on that
        page, so the extra dates cost one page load each per route, not one per
        carrier.
        """
        result = UnitResult(unit=unit, source=self.name, started_at=now_ts())
        last_error = ""
        found: dict = {}
        # Only populated when merge_cross_date is on: every non-empty ladder
        # seen for a cabin within the ACTIVE block, so the winner can be
        # patched with tiers a different date sold instead of the rest being
        # discarded outright. No extra scraping — these dates are walked
        # anyway; the only change is what happens to the ones that "lose".
        candidates: dict = {}
        # Opportunistic carriers (PC/VF) only try the primary date — no point
        # walking the whole fallback window for a carrier that isn't on the route.
        blocks = ([unit.date_plan.window[:1]] if unit.opportunistic
                  else unit.date_plan.blocks)
        for b_no, block in enumerate(blocks, start=1):
            # A carrier that flies < daily (common long-haul) legitimately has no
            # seat on some dates, so "flights exist but not this carrier" is only
            # terminal after it repeats — that's what the block is for.
            absent = 0
            absent_limit = 1 if unit.opportunistic else 5
            if self.merge_cross_date:
                candidates = {}          # a new block starts a fresh pool
            for dep, ret in block:
                try:
                    cabins = await self.fetch_search(page, unit.job, dep, ret)
                except CarrierAbsent as e:
                    last_error = str(e)
                    absent += 1
                    if absent >= absent_limit and not found:
                        break             # carrier really isn't on this route
                    continue
                except NoAvailabilityError as e:
                    last_error = str(e)
                    _LOG.debug("%s %s %s: no availability on %s", self.name,
                               unit.job.route, unit.date_plan.season.value, dep)
                    continue
                for c in cabins or []:
                    if not c.brands:
                        continue
                    if self.merge_cross_date:
                        candidates.setdefault(c.cabin, []).append(c)
                    prev = found.get(c.cabin)
                    # New cabin, or a strictly better ladder for one we have.
                    if prev is None or ladder_strength(c) > ladder_strength(prev):
                        found[c.cabin] = c
            if found:
                if self.merge_cross_date:
                    found = self._merge_cross_date_candidates(unit, found, candidates)
                # Later blocks exist ONLY for units the deep window cannot serve
                # (no schedule loaded that far out) — never as extra work.
                if len(blocks) > 1:
                    _LOG.info("%s %s %s: far-block=%d", self.name, unit.job.route,
                              unit.date_plan.season.value, b_no)
                break
        if found:
            result.cabin_results = list(found.values())
            result.status = UnitStatus.SUCCESS
            result.finished_at = now_ts()
            return result
        # Nothing in the whole window.
        result.status = UnitStatus.NO_AVAILABILITY
        result.error = last_error or "no availability in date window"
        result.finished_at = now_ts()
        return result

    def _merge_cross_date_candidates(self, unit: ScrapeUnit, found: dict,
                                     candidates: dict) -> dict:
        """Patch each cabin's winning ladder with tiers only OTHER sampled
        dates sold. Pure post-processing over dates already walked this
        block — see :meth:`run_unit`'s docstring for the rationale.
        """
        from ..normalization import merge_cross_date_ladder
        out = dict(found)
        for cab, winner in found.items():
            others = [(c.departure, c.brands) for c in candidates.get(cab, [])
                      if c is not winner]
            if not others:
                continue
            merged_brands, notes = merge_cross_date_ladder(winner.brands, others, self.name)
            if notes:
                winner.brands = merged_brands
                _LOG.info("Cross-date merge: %s %s %s +%s", self.name, unit.job.route,
                          cab.value, "; ".join(notes))
            out[cab] = winner
        return out


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
        for attr in ("_cache", "_result_cache", "_slug_cache", "_locks", "_result_locks",
                     "_dead_routes", "_hydrate_misses"):
            v = getattr(type(s), attr, None)
            if isinstance(v, (dict, set)):
                v.clear()
        type(s).reset_state()


def get_sources_for(carrier: str, only_names: Optional[list[str]] = None) -> list[SourceAdapter]:
    """Adapters that can serve ``carrier``, ordered by priority (fallback chain)."""
    out = []
    for s in registered_sources():
        if only_names and s.name not in only_names:
            continue
        if s.supports(carrier):
            out.append(s)
    return out
