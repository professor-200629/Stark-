"""
Structured incident ledger.

Hindsight holds the *reasoning* substrate (facts, observations, mental models).
This ledger holds the small amount of structured bookkeeping that charts need —
MTTR per incident, which family it belonged to, when it was first seen. Keeping
them separate means the memory layer stays swappable and the analytics stay
honest: nothing here is used to answer a triage question.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .seed_data import INCIDENTS


@dataclass
class LedgerEntry:
    incident_id: str
    opened_at: str
    service: str
    failure_class: str
    mttr_minutes: int
    severity: str
    responder: str
    had_prior_family_incident: bool = False
    source: str = "seed"


class IncidentLedger:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.entries: list[LedgerEntry] = []
        if path.exists():
            self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self.entries = [LedgerEntry(**e) for e in raw]
        except Exception:
            self.entries = []

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps([e.__dict__ for e in self.entries], indent=2), encoding="utf-8"
        )

    def reset(self) -> None:
        self.entries = []
        if not self.path.exists():
            return
        try:
            self.path.unlink()
        except OSError:  # some mounted folders forbid unlink; truncating is equivalent
            self.path.write_text("[]", encoding="utf-8")

    def seed(self) -> None:
        self.entries = []
        seen_families: set[str] = set()
        for incident in sorted(INCIDENTS, key=lambda i: i["opened_at"]):
            family = incident["failure_class"]
            self.entries.append(
                LedgerEntry(
                    incident_id=incident["incident_id"],
                    opened_at=incident["opened_at"],
                    service=incident["service"],
                    failure_class=family,
                    mttr_minutes=int(incident["mttr_minutes"]),
                    severity=incident["severity"],
                    responder=incident["responder"],
                    had_prior_family_incident=family in seen_families,
                )
            )
            seen_families.add(family)
        self.save()

    def add(self, entry: LedgerEntry) -> None:
        families = {e.failure_class for e in self.entries}
        entry.had_prior_family_incident = entry.failure_class in families
        self.entries.append(entry)
        self.save()

    # -- analytics ---------------------------------------------------------- #

    def mttr_summary(self) -> dict[str, Any]:
        first_time = [e.mttr_minutes for e in self.entries if not e.had_prior_family_incident]
        repeats = [e.mttr_minutes for e in self.entries if e.had_prior_family_incident]
        summary: dict[str, Any] = {
            "first_of_family_count": len(first_time),
            "repeat_count": len(repeats),
            "first_of_family_median_mttr": statistics.median(first_time) if first_time else None,
            "repeat_median_mttr": statistics.median(repeats) if repeats else None,
            "first_of_family_mean_mttr": round(statistics.fmean(first_time), 1) if first_time else None,
            "repeat_mean_mttr": round(statistics.fmean(repeats), 1) if repeats else None,
        }
        if summary["first_of_family_mean_mttr"] and summary["repeat_mean_mttr"]:
            a = summary["first_of_family_mean_mttr"]
            b = summary["repeat_mean_mttr"]
            summary["mttr_reduction_pct"] = round(100 * (a - b) / a, 1)
            # The literal arithmetic, so the number can be checked by hand.
            summary["derivation_mean"] = f"({a} - {b}) / {a} = {summary['mttr_reduction_pct']}%"

            # The mean is dragged around by three long-tail incidents (194, 210 and
            # 320 minutes), so the median is the number to lead with. Both are
            # reported rather than whichever happens to look better.
            ma = summary["first_of_family_median_mttr"]
            mb = summary["repeat_median_mttr"]
            summary["mttr_reduction_pct_median"] = round(100 * (ma - mb) / ma, 1)
            summary["derivation_median"] = (
                f"({ma} - {mb}) / {ma} = {summary['mttr_reduction_pct_median']}%"
            )
            summary["headline"] = "median"
            summary["outlier_note"] = (
                "Three first-of-family incidents were long-tail (194, 210 and 320 minutes: a "
                "duplicate-invoice data bug, a poison-message loop with no alert coverage, and a "
                "bundle-size regression nobody paged on). They pull the mean up. The median is "
                "the more honest headline."
            )
        else:
            summary["mttr_reduction_pct"] = None
            summary["mttr_reduction_pct_median"] = None
            summary["derivation_mean"] = None
            summary["derivation_median"] = None
        summary["definition"] = (
            "An incident is 'first of family' if no earlier incident in the corpus shares its "
            "failure_class; otherwise it is a 'repeat'. Both means are over the recorded "
            "mttr_minutes field of the seeded corpus. No model output is involved in this number."
        )
        summary["data_source"] = "synthetic corpus (app/seed_data.py), 21 incidents"
        summary["first_of_family_mttrs"] = sorted(
            e.mttr_minutes for e in self.entries if not e.had_prior_family_incident
        )
        summary["repeat_mttrs"] = sorted(
            e.mttr_minutes for e in self.entries if e.had_prior_family_incident
        )
        return summary

    def timeline(self) -> list[dict[str, Any]]:
        return [
            {
                "incident_id": e.incident_id,
                "opened_at": e.opened_at,
                "service": e.service,
                "failure_class": e.failure_class,
                "mttr_minutes": e.mttr_minutes,
                "repeat": e.had_prior_family_incident,
                "severity": e.severity,
            }
            for e in sorted(self.entries, key=lambda x: x.opened_at)
        ]

    def by_family(self) -> list[dict[str, Any]]:
        families: dict[str, list[LedgerEntry]] = {}
        for entry in sorted(self.entries, key=lambda x: x.opened_at):
            families.setdefault(entry.failure_class, []).append(entry)
        out = []
        for family, entries in families.items():
            out.append(
                {
                    "failure_class": family,
                    "occurrences": len(entries),
                    "services": sorted({e.service for e in entries}),
                    "mttr_series": [e.mttr_minutes for e in entries],
                    "incident_ids": [e.incident_id for e in entries],
                }
            )
        return sorted(out, key=lambda f: f["occurrences"], reverse=True)
