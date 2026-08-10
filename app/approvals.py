"""
The human approval loop.

STARK proposes; a person decides. Every decision is written back to memory as an
experience fact, which turns the system into something more interesting than a
retrieval engine: it accumulates a record of *which of its own recommendations
engineers actually trusted, and whether those recommendations worked.*

That is a second learning signal, orthogonal to the incident history:

    incident history  ->  what the system did
    decision history  ->  what this team thinks STARK should do

A recommendation that was rejected twice with a reason attached is worth
demoting even if retrieval still ranks its source incident first. A
recommendation that was approved and confirmed to work is worth leading with.

Recommendation keys are deliberately stable — a hash over (service,
failure_class, normalised action) — so the same proposed action accumulates a
track record across different alerts of the same family, rather than starting
from zero every time an alert fires.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

APPROVE = "approve"
REJECT = "reject"
INVESTIGATE = "investigate"
DECISIONS = (APPROVE, REJECT, INVESTIGATE)

WORKED = "worked"
DID_NOT_WORK = "did_not_work"
UNKNOWN = "unknown"
OUTCOMES = (WORKED, DID_NOT_WORK, UNKNOWN)

#: Risk is a property of the action, not of the model's confidence. These are the
#: verbs that change production state versus the ones that only look at it.
_HIGH_RISK = re.compile(
    r"\b(roll ?back|restart|redeploy|deploy|failover|fail over|scale|kill|drain|"
    r"purge|delete|revert|cut ?over|promote|reset|flip|disable|enable)\b",
    re.I,
)
_LOW_RISK = re.compile(
    r"\b(check|inspect|verify|confirm|look at|review|measure|monitor|determine|"
    r"establish|read|compare|audit)\b",
    re.I,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalise(action: str) -> str:
    """Collapse an action to a comparable form so the same advice maps to one key."""
    text = (action or "").lower()
    text = re.sub(r"\d+", "#", text)          # 20 -> #, 80 -> #: same advice, tuned differently
    text = re.sub(r"[^a-z#\s]", " ", text)
    return " ".join(text.split())[:220]


def recommendation_key(service: str, failure_class: str, action: str) -> str:
    raw = f"{service or '?'}::{failure_class or '?'}::{_normalise(action)}"
    return "REC-" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]


def classify_risk(action: str) -> str:
    """How much damage can this do if STARK is wrong?"""
    if _HIGH_RISK.search(action or ""):
        return "high"
    if _LOW_RISK.search(action or ""):
        return "low"
    return "medium"


@dataclass
class Recommendation:
    key: str
    action: str
    rationale: str
    evidence: list[str]
    source_incident: str
    service: str
    failure_class: str
    risk: str
    expected_impact: str
    track_record: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Decision:
    recommendation_key: str
    decision: str
    action: str
    service: str
    failure_class: str
    decided_by: str
    note: str = ""
    decided_at: str = field(default_factory=_now)
    outcome: str = UNKNOWN
    outcome_note: str = ""
    outcome_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class DecisionLog:
    """Append-only record of what humans did with STARK's recommendations."""

    def __init__(self, path: Path | None = None) -> None:
        self._lock = threading.RLock()
        self.path = path
        self.decisions: list[Decision] = []
        if path and path.exists():
            self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8") or "[]")
            self.decisions = [Decision(**d) for d in raw]
        except Exception:
            self.decisions = []

    def save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps([d.to_dict() for d in self.decisions], indent=2), encoding="utf-8"
        )

    def reset(self) -> None:
        with self._lock:
            self.decisions = []
            if self.path and self.path.exists():
                try:
                    self.path.unlink()
                except OSError:  # mounted folders sometimes forbid unlink
                    self.path.write_text("[]", encoding="utf-8")

    def add(self, decision: Decision) -> Decision:
        with self._lock:
            self.decisions.append(decision)
            self.save()
            return decision

    def record_outcome(self, key: str, outcome: str, note: str = "") -> Decision | None:
        """Attach an outcome to the most recent approval of this recommendation."""
        with self._lock:
            for decision in reversed(self.decisions):
                if decision.recommendation_key == key and decision.decision == APPROVE:
                    decision.outcome = outcome
                    decision.outcome_note = note
                    decision.outcome_at = _now()
                    self.save()
                    return decision
            return None

    # -- track records ------------------------------------------------------ #

    def track_record(self, key: str) -> dict[str, Any]:
        """What has this team done with this recommendation before?"""
        with self._lock:
            mine = [d for d in self.decisions if d.recommendation_key == key]
        if not mine:
            return {"seen": 0}

        approved = [d for d in mine if d.decision == APPROVE]
        rejected = [d for d in mine if d.decision == REJECT]
        investigated = [d for d in mine if d.decision == INVESTIGATE]
        worked = [d for d in approved if d.outcome == WORKED]
        failed = [d for d in approved if d.outcome == DID_NOT_WORK]

        last_reject = next((d for d in reversed(rejected) if d.note), None)
        return {
            "seen": len(mine),
            "approved": len(approved),
            "rejected": len(rejected),
            "investigated": len(investigated),
            "worked": len(worked),
            "did_not_work": len(failed),
            "last_rejection_note": last_reject.note if last_reject else "",
            "last_decided_at": mine[-1].decided_at,
            "summary": _summarise(len(approved), len(rejected), len(worked), len(failed)),
        }

    def stats(self) -> dict[str, Any]:
        with self._lock:
            total = len(self.decisions)
            approved = sum(1 for d in self.decisions if d.decision == APPROVE)
            rejected = sum(1 for d in self.decisions if d.decision == REJECT)
            investigated = sum(1 for d in self.decisions if d.decision == INVESTIGATE)
            worked = sum(1 for d in self.decisions if d.outcome == WORKED)
            failed = sum(1 for d in self.decisions if d.outcome == DID_NOT_WORK)
        return {
            "decisions": total,
            "approved": approved,
            "rejected": rejected,
            "investigated": investigated,
            "confirmed_worked": worked,
            "confirmed_failed": failed,
            "approval_rate": round(approved / total, 3) if total else None,
            "outcome_confirmed_rate": round((worked + failed) / approved, 3) if approved else None,
        }

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            return [d.to_dict() for d in reversed(self.decisions[-limit:])]


def _summarise(approved: int, rejected: int, worked: int, failed: int) -> str:
    bits: list[str] = []
    if approved:
        bits.append(f"approved {approved}x")
    if rejected:
        bits.append(f"rejected {rejected}x")
    if worked:
        bits.append(f"confirmed to work {worked}x")
    if failed:
        bits.append(f"did not work {failed}x")
    return ", ".join(bits) or "no prior decisions"


def rank_score(track: dict[str, Any]) -> float:
    """
    How much should a track record move a recommendation up or down the list?

    Deliberately blunt and readable rather than clever: a confirmed success is
    worth more than an approval, a confirmed failure hurts more than a rejection,
    and a recommendation nobody has ever ruled on is neutral.
    """
    if not track or not track.get("seen"):
        return 0.0
    return (
        2.0 * track.get("worked", 0)
        + 1.0 * track.get("approved", 0)
        - 1.5 * track.get("rejected", 0)
        - 2.5 * track.get("did_not_work", 0)
    )


def decision_memory(decision: Decision, alert_title: str = "") -> tuple[str, dict[str, Any]]:
    """
    Render a decision as a sentence worth remembering, plus its metadata.

    Written in the first person of the team, because at recall time the useful
    framing is "we decided this before", not "a row exists in a table".
    """
    verb = {
        APPROVE: "approved",
        REJECT: "rejected",
        INVESTIGATE: "chose to investigate rather than immediately apply",
    }[decision.decision]

    text = (
        f"On {decision.decided_at[:10]}, {decision.decided_by} {verb} STARK's recommendation "
        f"for {decision.service} ({decision.failure_class}): {decision.action}"
    )
    if decision.note:
        text += f" Reason given: {decision.note}"
    if decision.outcome == WORKED:
        text += " The action was later confirmed to have resolved the incident."
    elif decision.outcome == DID_NOT_WORK:
        text += " The action was later confirmed NOT to have resolved the incident."
    if alert_title:
        text += f" Alert at the time: {alert_title}"

    metadata = {
        "kind": "decision",
        "decision": decision.decision,
        "recommendation_key": decision.recommendation_key,
        "service": decision.service,
        "failure_class": decision.failure_class,
        "decided_by": decision.decided_by,
        "outcome": decision.outcome,
        "action": decision.action,
    }
    return text, metadata


def build_recommendations(
    brief: dict[str, Any],
    service: str,
    log: DecisionLog,
    limit: int = 3,
) -> list[Recommendation]:
    """
    Turn the brief's first actions into decisions a human can actually make.

    Only memory-grounded actions become recommendations. If STARK has no
    precedent it has no business asking anyone to approve a production change,
    so the approval panel stays empty and the brief stands as advice only.
    """
    if not brief.get("memory_grounded"):
        return []

    recalled = brief.get("recalled_incidents") or []
    failure_class = next(
        (c.get("failure_class") for c in recalled if c.get("failure_class")), ""
    )
    mttrs = [c.get("mttr_minutes") for c in recalled if isinstance(c.get("mttr_minutes"), int)]
    median_mttr = sorted(mttrs)[len(mttrs) // 2] if mttrs else None

    out: list[Recommendation] = []
    seen: set[str] = set()
    for action in brief.get("first_actions") or []:
        step = (action.get("step") or "").strip()
        if not step:
            continue
        key = recommendation_key(service, failure_class, step)
        if key in seen:
            continue
        seen.add(key)

        source = action.get("source_incident") or ""
        evidence = [source] if source else []
        for cause in brief.get("likely_root_causes") or []:
            for inc in cause.get("supporting_incidents") or []:
                if inc and inc not in evidence:
                    evidence.append(inc)

        impact = (
            f"Resolved {source} in {_mttr_for(source, recalled)} minutes."
            if source and _mttr_for(source, recalled)
            else (
                f"Similar incidents in this family were resolved in about {median_mttr} minutes."
                if median_mttr
                else "Based on how this failure family was resolved previously."
            )
        )

        out.append(
            Recommendation(
                key=key,
                action=step,
                rationale=action.get("rationale") or "",
                evidence=evidence[:4],
                source_incident=source,
                service=service,
                failure_class=failure_class,
                risk=classify_risk(step),
                expected_impact=impact,
                track_record=log.track_record(key),
            )
        )

    # A recommendation this team has rejected before should not keep leading the
    # list just because retrieval likes its source incident.
    out.sort(key=lambda r: rank_score(r.track_record), reverse=True)
    top = out[:limit]

    # ...but do not silently drop it either. A recommendation that was ruled on
    # before is informative precisely because it was demoted, so it stays visible
    # at the bottom carrying the reason it fell.
    demoted = [r for r in out[limit:] if r.track_record.get("seen")]
    for rec in demoted[:2]:
        rec.track_record = {**rec.track_record, "demoted": True}
    return top + demoted[:2]


def _mttr_for(incident_id: str, recalled: Iterable[dict[str, Any]]) -> int | None:
    for card in recalled:
        if card.get("incident_id") == incident_id and isinstance(card.get("mttr_minutes"), int):
            return card["mttr_minutes"]
    return None
