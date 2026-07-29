"""Async orchestrator: plan/resume -> parallel scrape -> validate -> write."""

from __future__ import annotations

import asyncio
import random
import time
from datetime import datetime
from typing import Any

from .amenities import empty_amenity_map, map_label_to_canonical
from .browser_pool import BrowserPool, NullPool
from .checkpoint import Checkpoint
from .config import Config
from .dates import build_date_plan
from .io_utils import (read_jobs, write_failed, write_normalized, write_raw, write_summary)
from .logging_setup import EventLog, setup_logging
from .models import (AmenityStatus, BrandedFare, Cabin, CabinResult, Job, PriceType,
                     RunSummary, ScrapeUnit, UnitResult, UnitStatus, now_ts)
from .normalization import (enrich_brands, iter_unit_ranked_by_cabin, ladder_metrics,
                            tier_code)
from .report import write_report
from .rebuild import (iter_raw_records, pair_prefs_for, season_pair_prefs_from_results,
                      unit_result_from_raw)
from .retry import retry_async
from .validation import apply_status, validate_unit
from . import sources as _sources  # registers adapters
from .sources.base import get_sources_for, reset_caches as reset_source_caches

_STATUS_RANK = {
    AmenityStatus.INCLUDED: 3, AmenityStatus.PAID: 2,
    AmenityStatus.NOT_INCLUDED: 1, AmenityStatus.UNKNOWN: 0,
}


class Runner:
    def __init__(self, config: Config):
        self.cfg = config
        config.ensure_dirs()
        self.log = setup_logging(config.logs_dir, config.log_level, config.run_id)
        self.events = EventLog(config.logs_dir / f"events_{config.run_id}.jsonl")
        self.checkpoint = Checkpoint(config.state_dir)
        self.summary = RunSummary(
            run_id=config.run_id,
            started_at=datetime.now().isoformat(timespec="seconds"),
        )

    # ------------------------------------------------------------------ #
    async def run(self) -> RunSummary:
        t0 = time.monotonic()
        reset_source_caches()               # fresh caches per run (no stale/dead-loop state)
        # Collection-moment evidence: every actual OTA search saves one page shot.
        from .sources.ubfly import Ubfly
        ev_dir = self.cfg.out / "evidence" / "searches"
        ev_dir.mkdir(parents=True, exist_ok=True)
        Ubfly.evidence_dir = ev_dir
        units = self._build_or_resume_units()
        self.summary.total_units = len(units)
        self.summary.total_jobs = len({u.job.key() for u in units})

        pending = [u for u in units if not (not self.cfg.fresh and self.checkpoint.is_done(u))]
        self.summary.skipped_checkpoint = len(units) - len(pending)
        self.log.info("Units: %d total, %d pending, %d skipped (checkpoint)",
                      len(units), len(pending), self.summary.skipped_checkpoint)

        need_browser = any(
            s.needs_browser
            for u in pending for s in get_sources_for(u.job.carrier, self.cfg.sources or None)
        )
        pool = BrowserPool(self.cfg) if need_browser else NullPool()
        if need_browser:
            self.log.info("Starting browser pool (size=%d, headless=%s)",
                          self.cfg.browser_pool_size, self.cfg.headless)
            await pool.start()

        sem = asyncio.Semaphore(self.cfg.concurrency)
        try:
            results = await asyncio.gather(*(self._process_unit(u, pool, sem) for u in pending))
            if self.cfg.validation_retry:
                results = await self._retry_incomplete(results, pool, sem)
        finally:
            await pool.stop()

        # Resume: units skipped via checkpoint had their data written by the prior
        # run only into raw_data.jsonl. Reload them so the rewritten outputs contain
        # BOTH prior and new units instead of truncating to just this run's.
        results = list(results) + self._load_prior_done_units(units, pending)

        self._write_outputs(results)
        self.summary.total_seconds = round(time.monotonic() - t0, 2)
        self.summary.finished_at = datetime.now().isoformat(timespec="seconds")
        write_summary(self.summary, self.cfg.out)
        self._print_summary()
        return self.summary

    # ------------------------------------------------------------------ #
    def _build_or_resume_units(self) -> list[ScrapeUnit]:
        if not self.cfg.fresh and self.checkpoint.has_plan():
            units = self.checkpoint.load_plan()
            self.log.info("Resuming frozen plan: %d units (%d already completed)",
                          len(units), self.checkpoint.completed_count())
            return units
        if self.cfg.fresh:
            self.checkpoint.reset()
        jobs = read_jobs(self.cfg.input_path)
        self.log.info("Loaded %d job rows from %s", len(jobs), self.cfg.input_path)
        rng = random.Random(self.cfg.seed)
        today = datetime.now().date()

        # Dates are generated per (OND, season) and shared by every carrier on
        # that OND, so all carriers hit the same OTA search (one cached fetch).
        ond_plans: dict[tuple, Any] = {}

        def plan_for(origin: str, destination: str, season) -> Any:
            k = (origin, destination, season)
            if k not in ond_plans:
                ond_plans[k] = build_date_plan(
                    season, today, min_lead_days=self.cfg.min_lead_days,
                    horizon_days=self.cfg.horizon_days, trip_length=self.cfg.trip_length,
                    window_days=self.cfg.window_days, rng=rng)
            return ond_plans[k]

        units: list[ScrapeUnit] = []
        for job in jobs:
            for season in self.cfg.seasons:
                units.append(ScrapeUnit(job=job, date_plan=plan_for(job.origin, job.destination, season)))

        # Opportunistic carriers (PC/VF): add for every distinct OND that doesn't
        # already list them; they yield rows only where they actually operate.
        onds = {(j.origin, j.destination) for j in jobs}
        listed = {(j.carrier.upper(), j.origin, j.destination) for j in jobs}
        for carrier in self.cfg.opportunistic_carriers:
            cu = carrier.upper()
            for origin, destination in sorted(onds):
                if (cu, origin, destination) in listed:
                    continue
                for season in self.cfg.seasons:
                    job = Job(carrier=cu, origin=origin, destination=destination,
                              raw_row={"OND": f"{origin}-{destination}", "CARRIER": cu,
                                       "_opportunistic": True})
                    units.append(ScrapeUnit(job=job, date_plan=plan_for(origin, destination, season),
                                            opportunistic=True))

        self.log.info("Built %d units (%d ONDs, +opportunistic %s)",
                      len(units), len(onds), ",".join(self.cfg.opportunistic_carriers))
        self.checkpoint.save_plan(units, self.cfg.run_id)
        return units

    def _load_prior_done_units(self, units: list[ScrapeUnit], pending: list[ScrapeUnit]) -> list[UnitResult]:
        if self.cfg.fresh or not self.summary.skipped_checkpoint:
            return []
        raw_path = self.cfg.out / "raw_data.jsonl"
        if not raw_path.exists():
            return []
        pending_keys = {u.key() for u in pending}
        done_keys = {u.key() for u in units} - pending_keys
        unit_by_key = {u.key(): u for u in units}
        out: list[UnitResult] = []
        seen: set[str] = set()
        try:
            for rec in iter_raw_records(raw_path):
                k = rec.get("unit_key")
                if k in done_keys and k not in seen:
                    ur = unit_result_from_raw(rec, unit_by_key)
                    if ur is not None:
                        out.append(ur)
                        seen.add(k)
        except Exception as e:  # noqa: BLE001 - prior file unreadable: skip, don't crash
            self.log.warning("Resume: could not reload prior raw_data (%s)", e)
            return []
        if out:
            self.log.info("Resume: reloaded %d completed unit(s) from prior raw_data.jsonl", len(out))
        return out

    # ------------------------------------------------------------------ #
    async def _process_unit(self, unit: ScrapeUnit, pool, sem: asyncio.Semaphore) -> UnitResult:
        async with sem:
            adapters = get_sources_for(unit.job.carrier, self.cfg.sources or None)
            if not adapters:
                r = UnitResult(unit=unit, status=UnitStatus.FAILED,
                               error=f"no adapter registered for carrier '{unit.job.carrier}'",
                               started_at=now_ts(), finished_at=now_ts())
                self.events.emit("no_adapter", carrier=unit.job.carrier, origin=unit.job.origin,
                                 destination=unit.job.destination, season=unit.date_plan.season.value)
                self.log.warning("No adapter for carrier '%s' (%s)", unit.job.carrier, unit.job.route)
                return r

            # Run the WHOLE source chain and merge per cabin: every source's
            # ladder becomes a candidate. Per cabin, the primary is the best
            # ladder (most fares, fewest implausible >3x jumps, smallest worst
            # step, then source priority); the losing sources then ENRICH the
            # primary's brands — filling missing amenity keys and miles on the
            # name-matched fare — so e.g. TK keeps Enuygun's ladder but gains
            # Ubfly's miles/same-day/fast-track rows.
            candidates: dict[Cabin, list[tuple[int, "CabinResult"]]] = {}   # noqa: F821
            tried: list[str] = []
            last: UnitResult | None = None
            total_retry = 0
            t0 = now_ts()
            for i, adapter in enumerate(adapters):
                tried.append(adapter.name)
                result = await self._run_with_retry(adapter, unit, pool)
                total_retry += result.retry_count
                last = result
                for cr in result.cabin_results:
                    if cr.brands:
                        cr.source = adapter.name
                        candidates.setdefault(cr.cabin, []).append((i, cr))

            merged: dict[Cabin, "CabinResult"] = {}   # noqa: F821
            for cab, cands in candidates.items():
                def qkey(t):
                    prio, cr = t
                    bad, worst = ladder_metrics(cr.brands)
                    return (-len(cr.brands), bad, round(worst, 2), prio)
                cands.sort(key=qkey)
                _, best_cr = cands[0]
                filled = sum(enrich_brands(best_cr.brands, other.brands)
                             for _, other in cands[1:])
                if filled:
                    self.log.info("Enrich: %s %s %s [%s] +%d fields from %s",
                                  unit.job.carrier, unit.job.route, cab.value,
                                  best_cr.source,
                                  filled, ",".join(o.source for _, o in cands[1:]))
                merged[cab] = best_cr

            if merged:
                out = UnitResult(
                    unit=unit, cabin_results=list(merged.values()), retry_count=total_retry,
                    sources_tried=list(tried), started_at=t0, finished_at=now_ts(),
                    source=",".join(sorted({cr.source for cr in merged.values()})))
                report = validate_unit(out, expected_min_cabins=1)
                out.validation_issues = report.issues
                apply_status(out, report)
                self.checkpoint.mark_done(unit)   # only cache real successes
                return out
            return last  # all sources exhausted, no data

    async def _run_with_retry(self, adapter, unit: ScrapeUnit, pool) -> UnitResult:
        attempts = {"n": 0}
        started = now_ts()
        self.events.emit("start", source=adapter.name, carrier=unit.job.carrier,
                         origin=unit.job.origin, destination=unit.job.destination,
                         season=unit.date_plan.season.value,
                         departure=str(unit.date_plan.departure))

        def _on_retry(attempt_no, exc, delay):
            attempts["n"] = attempt_no
            self.events.emit("retry", source=adapter.name, carrier=unit.job.carrier,
                             origin=unit.job.origin, destination=unit.job.destination,
                             attempt=attempt_no, delay=round(delay, 2), error=str(exc)[:300])
            self.log.warning("Retry %d for %s %s in %.1fs (%s)", attempt_no, adapter.name,
                             unit.job.route, delay, str(exc)[:120])

        async def _attempt() -> UnitResult:
            if adapter.needs_browser:
                async with pool.page() as page:
                    return await adapter.run_unit(page, unit)
            return await adapter.run_unit(None, unit)

        try:
            result = await retry_async(
                _attempt, max_retries=self.cfg.max_retries,
                base_delay=self.cfg.retry_base_delay, max_delay=self.cfg.retry_max_delay,
                on_retry=_on_retry)
        except asyncio.CancelledError:
            raise                     # let Ctrl-C / gather cancellation propagate
        except Exception as exc:  # noqa: BLE001
            result = UnitResult(unit=unit, source=adapter.name, status=UnitStatus.FAILED,
                                error=str(exc)[:500], started_at=started, finished_at=now_ts())
        result.retry_count = attempts["n"]
        self.events.emit("end", source=adapter.name, carrier=unit.job.carrier,
                         origin=unit.job.origin, destination=unit.job.destination,
                         season=unit.date_plan.season.value, status=result.status.value,
                         brands=result.total_brands(), retry=result.retry_count,
                         elapsed_s=result.elapsed, error=result.error[:300])
        self.log.info("%s | %s %s | %s | brands=%d | %.1fs%s",
                      adapter.name, unit.job.route, unit.date_plan.season.value,
                      result.status.value, result.total_brands(), result.elapsed,
                      f" | {result.error[:80]}" if result.error else "")
        return result

    async def _retry_incomplete(self, results: list[UnitResult], pool, sem) -> list[UnitResult]:
        redo_idx = [i for i, r in enumerate(results)
                    if r is not None and r.total_brands() == 0
                    and r.status in (UnitStatus.FAILED,)]
        if not redo_idx:
            return results
        self.log.info("Validation retry pass: re-attempting %d incomplete unit(s)", len(redo_idx))
        redone = await asyncio.gather(*(self._process_unit(results[i].unit, pool, sem) for i in redo_idx))
        for i, new in zip(redo_idx, redone):
            if new and new.total_brands() >= results[i].total_brands():
                results[i] = new
        return results

    # ------------------------------------------------------------------ #
    def _write_outputs(self, results: list[UnitResult]) -> None:
        results = [r for r in results if r is not None]
        # Cross-season ladder-order consensus, built from the very same in-memory
        # results that get written below, so this run's normalized_data.csv is
        # identical to what reprocess_raw/to_platform/make_excel later produce
        # from its raw_data.jsonl. (The ranking pass mutates the brand objects
        # but idempotently, so this pre-pass is safe.)
        prefs = season_pair_prefs_from_results(results)
        rows: list[BrandedFare] = []
        failed: list[UnitResult] = []
        for r in results:
            # Opportunistic carriers (PC/VF) that simply don't operate an OND are
            # expected — not counted, not written as failures.
            if r.unit.opportunistic and r.total_brands() == 0:
                self.summary.opportunistic_absent += 1
                continue
            self._tally(r)
            if r.total_brands() > 0:
                rows.extend(self._to_branded_fares(r, prefs))
            else:
                failed.append(r)

        write_raw(results, self.cfg.out)
        csv_path, xlsx_path = write_normalized(rows, self.cfg.out)
        write_failed(failed, self.cfg.out)
        self.summary.total_normalized_rows = len(rows)
        self.log.info("Wrote %d normalized rows -> %s%s", len(rows), csv_path,
                      f" (+ {xlsx_path.name})" if xlsx_path else "")
        if rows:
            report_path = write_report(rows, self.cfg.out)
            self.log.info("Wrote comparison report -> %s", report_path)

    def _tally(self, r: UnitResult) -> None:
        if r.status == UnitStatus.SUCCESS:
            self.summary.succeeded += 1
        elif r.status == UnitStatus.PARTIAL:
            self.summary.partial += 1
        elif r.status == UnitStatus.NO_AVAILABILITY:
            self.summary.no_availability += 1
        else:
            self.summary.failed += 1
        report = validate_unit(r)
        self.summary.units_missing_cabin += int(report.missing_cabin)
        self.summary.units_missing_brands += int(report.missing_brands)
        self.summary.units_missing_price += int(report.missing_price)
        self.summary.units_missing_amenities += int(report.missing_amenities)
        self.summary.units_missing_miles += int(report.missing_miles)
        self.summary.units_missing_source += int(report.missing_source)

    def _to_branded_fares(self, r: UnitResult, prefs: dict | None = None) -> list[BrandedFare]:
        out: list[BrandedFare] = []
        job, plan = r.unit.job, r.unit.date_plan
        ppc = pair_prefs_for(prefs or {}, job.carrier, job.origin, job.destination)
        # Re-bucket to each brand's effective cabin, order by price, and publish
        # exactly ONE ladder per cabin for the unit (``cab`` is the winning
        # ladder's own cabin result, for its dates/source).
        for eff_cab, raw, nb, order, absp, cab in iter_unit_ranked_by_cabin(
                r.cabin_results, carrier=job.carrier, pair_prefs_by_cabin=ppc):
            amap = empty_amenity_map()
            details: dict[str, str] = {}
            for a in raw.amenities:
                key = a.canonical_key or map_label_to_canonical(a.raw_label)
                if not key or key not in amap:
                    continue
                cur = AmenityStatus(amap[key])
                if _STATUS_RANK[a.status] >= _STATUS_RANK[cur]:
                    amap[key] = a.status.value
                    details[key] = a.raw_value or ""   # overwrite (clear stale detail)
            out.append(BrandedFare(
                carrier=job.carrier, origin=job.origin, destination=job.destination,
                departure_date=cab.departure or plan.departure,
                return_date=cab.return_date or plan.return_date,
                season=plan.season, cabin=eff_cab, brand_order=order,
                tier_code=tier_code(eff_cab, order),
                raw_brand_name=raw.raw_brand_name,
                normalized_brand_name=nb.normalized_name,
                display_price=raw.display_price_text or (str(raw.price_value) if raw.price_value is not None else ""),
                calculated_absolute_price=(absp if absp is not None
                                           else (raw.price_value if raw.price_type == PriceType.ABSOLUTE else None)),
                currency=raw.currency, source=cab.source or r.source or "",
                fare_family_code=raw.fare_family_code, brand_description=raw.description,
                amenities=amap, amenity_details=details,
                mileage_available=raw.miles.mileage_available,
                miles_earned=raw.miles.miles_earned,
                bonus_percent=raw.miles.bonus_percent,
                status=r.status.value, retry_count=r.retry_count,
            ))
        return out

    def _print_summary(self) -> None:
        s = self.summary
        self.log.info("=" * 62)
        self.log.info("RUN SUMMARY  (%s)", s.run_id)
        self.log.info("  jobs=%d  units=%d  skipped(ckpt)=%d", s.total_jobs, s.total_units, s.skipped_checkpoint)
        self.log.info("  success=%d  partial=%d  no_avail=%d  failed=%d",
                      s.succeeded, s.partial, s.no_availability, s.failed)
        self.log.info("  missing: cabin=%d brands=%d price=%d amenities=%d miles=%d source=%d",
                      s.units_missing_cabin, s.units_missing_brands, s.units_missing_price,
                      s.units_missing_amenities, s.units_missing_miles, s.units_missing_source)
        self.log.info("  normalized rows=%d  total=%.1fs", s.total_normalized_rows, s.total_seconds)
        self.log.info("=" * 62)


async def run_async(config: Config) -> RunSummary:
    return await Runner(config).run()


def run(config: Config) -> RunSummary:
    return asyncio.run(run_async(config))
