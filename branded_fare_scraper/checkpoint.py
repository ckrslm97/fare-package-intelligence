"""Frozen run-plan + resume support.

Two files under ``output/state/``:

* ``plan.json``       — the frozen plan (jobs + generated date plans). Written
  once at the start of a fresh run so that a *resumed* run reuses the exact same
  random dates (the spec: mid-run restart continues from the same place; only a
  brand-new run gets new random dates).
* ``checkpoint.json`` — the set of completed unit keys, updated as units finish.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from .models import DatePlan, Job, ScrapeUnit, Season


class Checkpoint:
    def __init__(self, state_dir: Path):
        self.state_dir = state_dir
        self.plan_path = state_dir / "plan.json"
        self.checkpoint_path = state_dir / "checkpoint.json"
        self._completed: set[str] = set()
        if self.checkpoint_path.exists():
            try:
                self._completed = set(json.loads(self.checkpoint_path.read_text("utf-8")).get("completed", []))
            except (json.JSONDecodeError, OSError):
                self._completed = set()

    # ----- plan persistence ------------------------------------------------ #
    def has_plan(self) -> bool:
        return self.plan_path.exists()

    def save_plan(self, units: list[ScrapeUnit], run_id: str) -> None:
        payload = {
            "run_id": run_id,
            "units": [
                {
                    "job": {
                        "carrier": u.job.carrier,
                        "origin": u.job.origin,
                        "destination": u.job.destination,
                        "raw_row": u.job.raw_row,
                        "job_id": u.job.job_id,
                    },
                    "date_plan": u.date_plan.to_dict(),
                    "opportunistic": u.opportunistic,
                }
                for u in units
            ],
        }
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.plan_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), "utf-8")

    def load_plan(self) -> list[ScrapeUnit]:
        payload = json.loads(self.plan_path.read_text("utf-8"))
        units: list[ScrapeUnit] = []
        for item in payload["units"]:
            j = item["job"]
            job = Job(
                carrier=j["carrier"], origin=j["origin"], destination=j["destination"],
                raw_row=j.get("raw_row", {}), job_id=j.get("job_id"),
            )
            units.append(ScrapeUnit(job=job, date_plan=DatePlan.from_dict(item["date_plan"]),
                                    opportunistic=item.get("opportunistic", False)))
        return units

    # ----- completion tracking -------------------------------------------- #
    def is_done(self, unit: ScrapeUnit) -> bool:
        return unit.key() in self._completed

    def mark_done(self, unit: ScrapeUnit) -> None:
        self._completed.add(unit.key())
        self._flush()

    def completed_count(self) -> int:
        return len(self._completed)

    def _flush(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.checkpoint_path.write_text(
            json.dumps({"completed": sorted(self._completed)}, ensure_ascii=False, indent=2),
            "utf-8",
        )

    def reset(self) -> None:
        self._completed.clear()
        for p in (self.plan_path, self.checkpoint_path):
            if p.exists():
                p.unlink()
