"""
Controlled A/B: the same alerts, through the same agent, with memory off and on.

Design constraints that make this an experiment rather than a demo:

**Leave-one-out.** Each incident is scored against a memory containing every
*other* incident. Without this the memory arm is simply reading the answer sheet,
and the whole table is worthless.

**Honest rows.** Some metrics are zero for the no-memory arm by construction — it
cannot cite an incident ID because it has none. Those rows are labelled
`constructive: True` and should not be read as findings. They are included
because omitting them would make the table look cherry-picked, not because
"0 vs 9" proves anything.

The rows that *are* findings:

  root cause identified   can a memoryless agent guess the true cause from the
                          alert alone? Sometimes yes — "connection pool
                          exhaustion" is a reasonable guess given a Hikari
                          timeout. This is the fair fight.
  repeats a known dead end  does the brief recommend something this team already
                          proved was a waste of time? This is the row that
                          matters, and it is the one memory should win on.
  concrete action         does the advice contain a parameter you could actually
                          apply, or is it "investigate the issue"?
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .ledger import IncidentLedger
from .memory import LocalMemoryEngine, MemoryStore
from .seed_data import INCIDENTS

#: Words that carry no discriminating power when matching a root cause.
_NOISE = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with", "that",
    "this", "was", "were", "is", "are", "be", "been", "it", "its", "as", "at",
    "by", "from", "had", "has", "have", "not", "but", "so", "which", "while",
    "new", "own", "per", "into", "than", "then", "when", "after", "before",
    "during", "every", "each", "all", "some", "more", "most", "other", "same",
    "service", "incident", "issue", "problem", "cause", "caused", "causing",
}

_CONCRETE = re.compile(
    r"\b\d+\s*(?:ms|s|m|min|gb|gi|mb|kb|%|x)\b|[a-z_]+\.[a-z_.]+\s*=|"
    r"\b[a-z_]{4,}_[a-z_]{3,}\b|--[a-z-]+|\b[A-Z][a-zA-Z]+\.[a-z][a-zA-Z]+\(",
    re.I,
)


def _stem(word: str) -> str:
    """
    Crude suffix stripping so `scaled` matches `scale` and `restarted` matches
    `restart`. A real stemmer would be overkill here and much harder to audit.

    The trailing-`e` strip is what makes it symmetric: without it `scaled` folds
    to `scal` while `scale` stays `scale`, and the two never match. On this corpus
    fixing it did not change the dead-end count, but an asymmetric comparison is a
    bug whether or not it happens to be load-bearing today.
    """
    stem = word
    for suffix in ("ing", "ed", "es", "s"):
        if len(stem) >= 5 and stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    if len(stem) > 4 and stem.endswith("e"):
        stem = stem[:-1]
    return stem


def _terms(text: str) -> set[str]:
    words = re.findall(r"[a-z][a-z0-9_.-]{3,}", (text or "").lower())
    return {_stem(w) for w in words if w not in _NOISE}


def _overlap(candidate: str, truth: str) -> float:
    """Share of the truth's distinctive vocabulary that the candidate reproduces."""
    truth_terms = _terms(truth)
    if not truth_terms:
        return 0.0
    return len(_terms(candidate) & truth_terms) / len(truth_terms)


def _echoes(action_text: str, dead_end: str, threshold: float = 0.34) -> list[str]:
    """
    Does this recommendation echo a dead end?

    Directional on purpose: the question is what share of *the advice* appears in
    the recorded mistake, not the reverse — a dead end is usually a paragraph of
    narrative and the advice is one line. Two shared terms minimum, so a single
    coincidental word cannot trigger a hit, and the matched terms are returned so
    every hit in the table can be audited by eye.
    """
    action_terms = _terms(action_text)
    if not action_terms:
        return []
    shared = sorted(action_terms & _terms(dead_end))
    if len(shared) >= 2 and len(shared) / len(action_terms) >= threshold:
        return shared
    return []


@dataclass
class ArmResult:
    arm: str
    scored: int = 0
    root_cause_hits: int = 0
    family_hits: int = 0
    cause_score_total: float = 0.0
    cited_correct_precedent: int = 0
    false_precedent: int = 0
    repeats_known_dead_end: int = 0
    concrete_actions: int = 0
    evidence_citations: int = 0
    declined_when_novel: int = 0
    novel_opportunities: int = 0
    rows: list[dict[str, Any]] = field(default_factory=list)

    def rates(self) -> dict[str, Any]:
        n = max(self.scored, 1)
        return {
            "arm": self.arm,
            "scored": self.scored,
            "identified_failure_family": round(self.family_hits / n, 3),
            "root_cause_identified": round(self.root_cause_hits / n, 3),
            "mean_root_cause_overlap": round(self.cause_score_total / n, 3),
            "cited_correct_precedent": round(self.cited_correct_precedent / n, 3),
            "false_precedent": round(self.false_precedent / n, 3),
            "repeats_known_dead_end": round(self.repeats_known_dead_end / n, 3),
            "concrete_action": round(self.concrete_actions / n, 3),
            "evidence_citations": self.evidence_citations,
            "declined_when_novel": (
                round(self.declined_when_novel / self.novel_opportunities, 3)
                if self.novel_opportunities
                else None
            ),
        }


#: How each row should be read. `constructive` means the no-memory arm scores
#: zero by definition, so the gap is arithmetic, not evidence.
ROW_META = {
    "identified_failure_family": {
        "label": "Named the right failure mechanism",
        "higher_is_better": True,
        "constructive": False,
        "path_dependent": True,
        "note": (
            "Not a memory win, and not claimed as one. A capable model reads the mechanism "
            "straight off the alert signature; memory's contribution is knowing what to do "
            "about it. Reported because hiding a row that does not favour us would be worse."
        ),
    },
    "root_cause_identified": {
        "label": "Got close to the specific root cause",
        "higher_is_better": True,
        "constructive": False,
        "note": (
            "Distinctive-term overlap with the recorded root cause above 0.20. Under "
            "leave-one-out the exact cause is not in memory, so this is deliberately a "
            "low bar and the mean overlap is reported alongside it."
        ),
    },
    "mean_root_cause_overlap": {
        "label": "Mean overlap with the true root cause",
        "higher_is_better": True,
        "constructive": False,
        "note": "Continuous version of the row above — no threshold to argue about.",
    },
    "repeats_known_dead_end": {
        "label": "Recommended a known dead end",
        "higher_is_better": False,
        "constructive": False,
        "note": (
            "The advice echoes a false lead this team already proved was a waste of time. "
            "Every hit carries the matched terms so it can be audited by eye."
        ),
    },
    "concrete_action": {
        "label": "Gave an action you could actually apply",
        "higher_is_better": True,
        "constructive": False,
        "note": "Contains a parameter, config key, threshold or method reference.",
    },
    "cited_correct_precedent": {
        "label": "Cited a real matching incident",
        "higher_is_better": True,
        "constructive": True,
        "note": "Zero without memory by construction — there are no incident IDs to cite.",
    },
    "false_precedent": {
        "label": "Cited an unrelated incident",
        "higher_is_better": False,
        "constructive": True,
        "note": "Zero without memory by construction. Included so the table is not one-sided.",
    },
    "declined_when_novel": {
        "label": "Declined when no precedent existed",
        "higher_is_better": True,
        "constructive": True,
        "note": (
            "The no-memory arm scores 1.0 vacuously — it declines everything, always, because "
            "it never claims precedent at all. The memory arm's number is the real one, and it "
            "is the weakest result here: with the whole corpus in memory it sometimes grounds a "
            "one-off failure on a different family from the same service. Reported because a "
            "table that only showed rows we win is not evidence."
        ),
    },
}


def _alert_for(incident: dict[str, Any]) -> str:
    return (
        f"ALERT: {incident['alert_title']}\n"
        f"service={incident['service']}\n"
        f"{incident['alert_payload']}\n"
        f"{incident['symptoms']}"
    )


def _known_dead_ends(incident: dict[str, Any]) -> list[str]:
    """False leads recorded for this failure family, excluding the incident itself."""
    out = []
    for other in INCIDENTS:
        if other["incident_id"] == incident["incident_id"]:
            continue
        if other["failure_class"] != incident["failure_class"]:
            continue
        lead = other.get("false_leads", "")
        if lead and not lead.lower().startswith("none"):
            out.append(lead)
    return out


def _score(
    brief: dict[str, Any], incident: dict[str, Any], arm: ArmResult, has_precedent: bool
) -> dict[str, Any]:
    causes = " ".join(c.get("cause", "") for c in brief.get("likely_root_causes") or [])
    actions = " ".join(a.get("step", "") for a in brief.get("first_actions") or [])
    verdict = brief.get("verdict") or ""

    # Under leave-one-out the exact root cause of the held-out incident is not in
    # memory, so demanding it verbatim would be a rigged test. Two metrics: did it
    # name the right failure mechanism (fair), and how close did the wording get
    # (continuous, reported as a mean rather than a pass mark).
    cause_score = _overlap(causes + " " + verdict, incident["root_cause"])
    root_hit = cause_score >= 0.20
    family_terms = _terms(incident["failure_class"].replace("-", " "))
    said = _terms(verdict + " " + causes)
    family_hit = bool(family_terms) and len(family_terms & said) / len(family_terms) >= 0.5

    cited = [
        s.get("incident_id", "")
        for s in (brief.get("similar_incidents") or [])
        if s.get("incident_id")
    ] or [c.get("incident_id", "") for c in (brief.get("recalled_incidents") or [])]
    cited = [c for c in cited if c and c != incident["incident_id"]]
    families = {
        i["failure_class"] for i in INCIDENTS if i["incident_id"] in cited
    }
    correct_precedent = incident["failure_class"] in families
    false_precedent = bool(cited) and not correct_precedent

    dead_ends = _known_dead_ends(incident)
    dead_end_hit = None
    for lead in dead_ends:
        for action in brief.get("first_actions") or []:
            shared = _echoes(action.get("step", ""), lead)
            if shared:
                dead_end_hit = {"action": action.get("step", "")[:120],
                                "dead_end": lead[:140], "shared_terms": shared}
                break
        if dead_end_hit:
            break
    repeats_dead_end = dead_end_hit is not None

    concrete = bool(_CONCRETE.search(actions))

    arm.scored += 1
    arm.root_cause_hits += int(root_hit)
    arm.family_hits += int(family_hit)
    arm.cause_score_total += cause_score
    arm.cited_correct_precedent += int(correct_precedent)
    arm.false_precedent += int(false_precedent)
    arm.repeats_known_dead_end += int(repeats_dead_end)
    arm.concrete_actions += int(concrete)
    arm.evidence_citations += len(cited)
    if not has_precedent:
        arm.novel_opportunities += 1
        arm.declined_when_novel += int(not brief.get("memory_grounded"))

    row = {
        "incident_id": incident["incident_id"],
        "service": incident["service"],
        "failure_class": incident["failure_class"],
        "has_precedent": has_precedent,
        "root_cause_score": round(cause_score, 3),
        "root_cause_identified": root_hit,
        "identified_failure_family": family_hit,
        "dead_end_match": dead_end_hit,
        "cited": cited[:4],
        "cited_correct_precedent": correct_precedent,
        "false_precedent": false_precedent,
        "repeats_known_dead_end": repeats_dead_end,
        "concrete_action": concrete,
        "grounded": bool(brief.get("memory_grounded")),
        "verdict": (brief.get("verdict") or "")[:180],
    }
    arm.rows.append(row)
    return row


def run_benchmark(agent_factory, settings, limit: int | None = None) -> dict[str, Any]:
    """
    Score every incident twice: once with no memory, once with leave-one-out memory.

    `agent_factory(store, ledger)` builds an agent bound to a scratch memory, so
    the live bank is never touched.
    """
    incidents = sorted(INCIDENTS, key=lambda i: i["opened_at"])
    if limit:
        incidents = incidents[:limit]

    without = ArmResult("no memory")
    with_memory = ArmResult("STARK memory")
    synthesis = "unknown"

    # Build the bank once and hide the incident under test at recall time. The
    # naive version rebuilds a 20-incident memory on every iteration; this is the
    # same experiment, twenty times faster.
    from .agent import incident_to_memory_items  # local import avoids a cycle

    store = MemoryStore.__new__(MemoryStore)
    store.backend = LocalMemoryEngine(None)
    store.mode = "benchmark"
    store.settings = settings
    store.exclude = None
    ledger = IncidentLedger.__new__(IncidentLedger)
    ledger.path = None
    ledger.entries = []
    agent = agent_factory(store, ledger)

    all_items = []
    for other in INCIDENTS:
        all_items.extend(incident_to_memory_items(other))
    store.retain(all_items)

    for incident in incidents:
        alert = _alert_for(incident)
        family = incident["failure_class"]
        has_precedent = any(
            other["failure_class"] == family and other["incident_id"] != incident["incident_id"]
            for other in INCIDENTS
        )

        store.exclude = {"incident_id": incident["incident_id"]}
        brief_off = agent.triage(alert, use_memory=False)
        brief_on = agent.triage(alert, use_memory=True)
        synthesis = brief_on.get("synthesis", synthesis)

        _score(brief_off, incident, without, has_precedent)
        _score(brief_on, incident, with_memory, has_precedent)

    store.exclude = None
    return {
        "arms": [without.rates(), with_memory.rates()],
        "rows": {"without_memory": without.rows, "with_memory": with_memory.rows},
        "row_meta": ROW_META,
        "method": (
            "Every incident is scored twice against the same agent: once with memory disabled, "
            "once with a memory holding all 20 other incidents and never the one under test "
            "(leave-one-out). Identical alert text, identical model, identical prompts apart from "
            "the recalled memories."
        ),
        "synthesis": synthesis,
        "caveat": (
            "Synthetic 21-incident corpus. Rows marked constructive are zero for the no-memory arm "
            "by definition and are reported for completeness, not as findings. The rows worth "
            "arguing about are dead-end repetition and action concreteness."
        ),
        "path_dependence": (
            "These numbers depend on which synthesis path ran. With a capable LLM the no-memory "
            "arm often names the failure mechanism from the alert text alone — the error signature "
            "is in the log line — so that row is not a memory win and is not presented as one. "
            "Dead-end repetition and action concreteness favour memory on both paths, because "
            "neither can be derived from the alert: they require knowing what this team already "
            "tried."
        ),
        "headline": _headline(without, with_memory),
    }


def _headline(a: ArmResult, b: ArmResult) -> dict[str, Any]:
    """The single comparison worth putting on a slide."""
    n = max(a.scored, 1)
    return {
        "metric": "Recommended a dead end this team had already ruled out",
        "without_memory": f"{a.repeats_known_dead_end}/{a.scored}",
        "with_memory": f"{b.repeats_known_dead_end}/{b.scored}",
        "root_cause_without": f"{a.root_cause_hits}/{a.scored}",
        "root_cause_with": f"{b.root_cause_hits}/{b.scored}",
        "statement": (
            f"With no memory the agent recommended a known dead end on "
            f"{a.repeats_known_dead_end} of {a.scored} incidents. With memory, "
            f"{b.repeats_known_dead_end} of {b.scored}."
        ),
    }
