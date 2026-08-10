"""
The STARK on-call agent.

Flow for every incoming alert:

    1. fingerprint  — pull service, entities and error signatures out of the raw alert
    2. recall       — ask Hindsight for everything the team has ever learned that
                      resembles this fingerprint (4-way retrieval, not a keyword grep)
    3. assemble     — group recalled facts back into incident cards + observations
    4. reason       — hand ONLY the recalled memory to the LLM and ask for a triage
                      brief that cites incident IDs; no memory, no citation
    5. retain       — when the responder records what actually happened, write it
                      back so the next alert of this shape is cheaper

Step 4 is deliberately memory-constrained: the system prompt forbids advice that
is not traceable to a recalled memory. That is what makes the with-memory /
without-memory comparison meaningful rather than cosmetic.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from . import llm
from .approvals import (
    Decision,
    DecisionLog,
    build_recommendations,
    decision_memory,
)
from .voice import speakable
from .ledger import IncidentLedger, LedgerEntry
from .memory import (
    EXPERIENCE,
    MENTAL_MODEL,
    OBSERVATION,
    WORLD,
    MemoryHit,
    MemoryItem,
    MemoryStore,
    extract_entities,
    normalize_text,
)
from .seed_data import INCIDENTS

KNOWN_SERVICES = sorted({i["service"] for i in INCIDENTS} | {"webhook-dispatcher", "checkout-web"})

SYSTEM_WITH_MEMORY = """You are STARK, the on-call memory of a payments platform SRE team.

You are given a live alert and a set of MEMORIES recalled from the team's incident history.
Produce a triage brief.

Hard rules:
- Every root cause, action, and warning you give MUST be traceable to a recalled memory.
  Cite the incident IDs that support it.
- If the memories do not resemble the alert, say so explicitly: set memory_grounded=false,
  leave similar_incidents empty, and give only generic first-response steps. Never
  force-fit an unrelated incident.
- Never invent an incident ID, runbook name, metric, or config value that is not in memory.
- Prefer the resolution with the best observed outcome, not merely the most recent.
- The "do_not_do" list is the most valuable part of the brief: it comes from false leads
  that cost the team real minutes in past incidents.

Return ONLY JSON matching this schema:
{
  "verdict": "one sentence: what this most likely is",
  "confidence": "high" | "medium" | "low",
  "memory_grounded": true | false,
  "similar_incidents": [{"incident_id": "", "why": "", "similarity": "high|medium|low"}],
  "likely_root_causes": [{"cause": "", "supporting_incidents": [""], "confidence": "high|medium|low"}],
  "first_actions": [{"step": "", "rationale": "", "source_incident": ""}],
  "do_not_do": [{"action": "", "reason": "", "source_incident": ""}],
  "estimated_mttr_minutes": 0,
  "open_questions": [""]
}"""

SYSTEM_WITHOUT_MEMORY = """You are a competent but memoryless SRE assistant. You have never
seen this system before and have no access to any incident history.

Given a live alert, produce a triage brief using only general engineering knowledge.
You have no incident IDs to cite, so similar_incidents must be empty, memory_grounded must
be false, and source_incident fields must be empty strings.

Return ONLY JSON matching this schema:
{
  "verdict": "",
  "confidence": "high" | "medium" | "low",
  "memory_grounded": false,
  "similar_incidents": [],
  "likely_root_causes": [{"cause": "", "supporting_incidents": [], "confidence": ""}],
  "first_actions": [{"step": "", "rationale": "", "source_incident": ""}],
  "do_not_do": [{"action": "", "reason": "", "source_incident": ""}],
  "estimated_mttr_minutes": 0,
  "open_questions": [""]
}"""


# --------------------------------------------------------------------------- #
# Turning incidents into memories
# --------------------------------------------------------------------------- #


def incident_to_memory_items(incident: dict) -> list[MemoryItem]:
    """
    Decompose one incident into the memories Hindsight should hold.

    World facts describe what was true about the system. Experience facts describe
    what this team *did* — the fix that worked and the false lead that cost time.
    Splitting them matters: during recall we want the agent's own past actions
    weighted differently from objective system behaviour.
    """
    iid = incident["incident_id"]
    service = incident["service"]
    family = incident["failure_class"]
    when = incident["opened_at"]

    base_meta = {
        "incident_id": iid,
        "service": service,
        "failure_class": family,
        "severity": incident["severity"],
        "responder": incident["responder"],
        "opened_at": when,
        "mttr_minutes": incident["mttr_minutes"],
        "fix": incident["fix"],
        "tags": incident.get("tags", []),
    }
    context = f"incident {iid} on {service} ({family})"

    def item(kind: str, text: str, mem_type: str = WORLD) -> MemoryItem:
        return MemoryItem(
            content=text,
            context=context,
            type=mem_type,
            timestamp=when,
            document_id=iid,
            metadata={**base_meta, "kind": kind},
        )

    items = [
        item("alert", f"{iid} ({incident['severity']}, {when[:10]}) on {service}: {incident['alert_title']}."),
        item("alert_payload", f"{iid} alert payload on {service}: " + incident["alert_payload"].replace("\n", " | ")),
        item("symptoms", f"{iid} symptoms on {service}: {incident['symptoms']}"),
        item("root_cause", f"{iid} root cause ({family}) on {service}: {incident['root_cause']}"),
        item("impact", f"{iid} customer impact: {incident['customer_impact']}"),
        item("verification", f"{iid} verification that the fix worked: {incident['verification']}"),
        item(
            "fix",
            f"{iid}: {incident['responder']} resolved this {family} incident on {service} in "
            f"{incident['mttr_minutes']} minutes by: {incident['fix']}",
            EXPERIENCE,
        ),
    ]
    if incident.get("false_leads") and not incident["false_leads"].lower().startswith("none"):
        items.append(
            item(
                "false_lead",
                f"{iid} false lead — time was wasted here, do not repeat it: {incident['false_leads']}",
                EXPERIENCE,
            )
        )
    return items


# --------------------------------------------------------------------------- #
# Alert fingerprinting
# --------------------------------------------------------------------------- #


def fingerprint(alert_text: str) -> dict[str, Any]:
    text = alert_text or ""
    service = ""
    match = re.search(r"service\s*[=:]\s*([a-z0-9\-]+)", text, re.I)
    if match:
        service = match.group(1).lower()
    else:
        # Alert names are usually CamelCase ("AuthGateway401Spike"); split them so
        # they can be matched against hyphenated service names.
        haystack = normalize_text(text).lower()
        haystack = re.sub(r"[\s\-]+", " ", haystack)
        for candidate in KNOWN_SERVICES:
            needle = candidate.lower().replace("-", " ")
            if needle in haystack:
                service = candidate
                break

    alert_name = ""
    match = re.search(r"ALERT:\s*([A-Za-z0-9_\-]+)", text)
    if match:
        alert_name = match.group(1)

    signatures = [
        line.strip()
        for line in text.splitlines()
        if re.search(r"\b(log|error|exception|log:)\b", line, re.I) or "Exception" in line
    ]
    return {
        "service": service,
        "alert_name": alert_name,
        "entities": sorted(extract_entities(text) | ({service} if service else set())),
        "signatures": signatures[:6],
    }


def build_recall_query(alert_text: str, fp: dict[str, Any]) -> str:
    parts: list[str] = []
    if fp["service"]:
        parts.append(f"incidents on {fp['service']}")
    if fp["alert_name"]:
        parts.append(fp["alert_name"])
    parts.extend(fp["signatures"])
    parts.append(" ".join(fp["entities"]))
    # keep the first few alert lines: they carry the metric + threshold shape
    parts.extend(alert_text.strip().splitlines()[:4])
    return " ".join(p for p in parts if p)[:1200]


# --------------------------------------------------------------------------- #
# Agent
# --------------------------------------------------------------------------- #


class StarkAgent:
    def __init__(
        self,
        store: MemoryStore,
        ledger: IncidentLedger,
        decisions: DecisionLog | None = None,
    ) -> None:
        self.store = store
        self.ledger = ledger
        self.decisions = decisions if decisions is not None else DecisionLog(None)

    # -- seeding ------------------------------------------------------------ #

    def seed(self, force: bool = True) -> dict[str, Any]:
        """Load the incident corpus. Idempotent: re-seeding clears first so the
        demo can be reset repeatedly without duplicating facts."""
        if force and self.store.stats().get("facts", 0):
            self.store.reset()
            self.ledger.reset()
            # Decisions are memories too. Re-seeding is a demo reset, so a stale
            # decision log must not survive it and silently reorder recommendations.
            self.decisions.reset()
        items: list[MemoryItem] = []
        for incident in sorted(INCIDENTS, key=lambda i: i["opened_at"]):
            items.extend(incident_to_memory_items(incident))
        self.store.retain(items)
        self._install_mental_models()
        self.ledger.seed()
        return {"incidents": len(INCIDENTS), "memories": len(items), **self.store.stats()}

    def _install_mental_models(self) -> None:
        """Curated summaries the team wants the agent to reach for first."""
        self.store.set_mental_model(
            "kafka consumer lag playbook",
            "For ledger-worker consumer lag: never restart or scale the consumer group while a "
            "rebalance is in progress (INC-1055, INC-1104 — both got worse). First determine "
            "whether rebalances are actually happening. If there is no rebalance churn and polls "
            "return zero records, suspect deserialization or schema registry, not poll timeouts "
            "(INC-1149).",
            {"services": ["ledger-worker"], "curated_by": "dana.whitfield"},
        )
        self.store.set_mental_model(
            "payments-api connection pool playbook",
            "payments-api latency cliffs with HikariPool timeouts are almost always pool "
            "saturation, not Aurora CPU (INC-1042, INC-1088, INC-1131). Check hikaricp_connections_pending "
            "and pgbouncer cl_waiting before touching the database. Aurora writer CPU below ~60% "
            "with high p99 is the tell.",
            {"services": ["payments-api"], "curated_by": "priya.raghavan"},
        )
        self.store.set_mental_model(
            "postgres replica lag rule",
            "Never fail over Aurora while replay lag is climbing (INC-1080, INC-1137). Find the "
            "long-running maintenance operation holding replay back and stop it instead.",
            {"services": ["pg-primary"], "curated_by": "dana.whitfield"},
        )

    # -- recall & assembly -------------------------------------------------- #

    def recall_for_alert(self, alert_text: str, limit: int = 18) -> tuple[dict[str, Any], list[MemoryHit]]:
        fp = fingerprint(alert_text)
        query = build_recall_query(alert_text, fp)
        hits = self.store.recall(query, limit=limit)
        return fp, hits

    @staticmethod
    def assemble(hits: list[MemoryHit]) -> dict[str, Any]:
        """Group flat memory hits back into incident cards, observations, models."""
        incidents: dict[str, dict[str, Any]] = defaultdict(
            lambda: {"incident_id": "", "score": 0.0, "facts": {}, "meta": {}}
        )
        observations: list[dict[str, Any]] = []
        mental_models: list[dict[str, Any]] = []

        for hit in hits:
            if hit.type == OBSERVATION:
                observations.append(
                    {
                        "text": hit.text,
                        "proof_count": hit.proof_count,
                        "score": hit.score,
                        "incident_ids": hit.metadata.get("incident_ids", []),
                        "recommended_fix": hit.metadata.get("recommended_fix"),
                    }
                )
                continue
            if hit.type == MENTAL_MODEL:
                mental_models.append({"text": hit.text, "key": hit.metadata.get("key", ""), "score": hit.score})
                continue

            iid = hit.metadata.get("incident_id") or _incident_id_in(hit.text)
            if not iid:
                continue
            card = incidents[iid]
            card["incident_id"] = iid
            card["score"] += hit.score
            kind = hit.metadata.get("kind", "note")
            card["facts"].setdefault(kind, []).append(hit.text)
            for key in ("service", "failure_class", "severity", "opened_at", "mttr_minutes", "fix", "responder"):
                if hit.metadata.get(key) and key not in card["meta"]:
                    card["meta"][key] = hit.metadata[key]

        ranked = sorted(incidents.values(), key=lambda c: c["score"], reverse=True)
        return {
            "incidents": ranked,
            "observations": sorted(observations, key=lambda o: o["score"], reverse=True),
            "mental_models": sorted(mental_models, key=lambda m: m["score"], reverse=True),
        }

    # -- second-hop enrichment & relevance gating --------------------------- #

    def enrich(self, assembled: dict[str, Any], top_n: int = 3) -> None:
        """
        Second retrieval hop.

        The first recall is scored against the whole alert, so a strongly matching
        incident may come back with only its alert line. Once we know *which*
        incidents matter, we go back to memory and ask for the parts that actually
        drive a triage decision — root cause, the fix, and the false lead. Cheap,
        and it is the difference between "this looks like INC-1149" and "here is
        exactly what INC-1149 turned out to be and what wasted 14 minutes."
        """
        for card in assembled["incidents"][:top_n]:
            iid = card["incident_id"]
            extra = self.store.recall(
                f"{iid} root cause fix false lead symptoms verification", limit=12
            )
            for hit in extra:
                if (hit.metadata.get("incident_id") or _incident_id_in(hit.text)) != iid:
                    continue
                kind = hit.metadata.get("kind", "note")
                bucket = card["facts"].setdefault(kind, [])
                if hit.text not in bucket:
                    bucket.append(hit.text)
                for key in ("service", "failure_class", "severity", "opened_at", "mttr_minutes", "fix", "responder"):
                    if hit.metadata.get(key) and key not in card["meta"]:
                        card["meta"][key] = hit.metadata[key]

    @staticmethod
    def score_relevance(fp: dict[str, Any], assembled: dict[str, Any]) -> bool:
        """
        Decide whether memory actually has something to say.

        An incident-response agent that pattern-matches an unrelated incident onto
        a novel alert is worse than one with no memory at all, so this gate is
        deliberately strict: either the service matches, or the alert and the past
        incident share enough distinctive vocabulary to justify the claim.
        """
        alert_entities = set(fp.get("entities", []))
        alert_tokens = set(_tokens(" ".join(fp.get("signatures", [])) + " " + " ".join(alert_entities)))

        for card in assembled["incidents"]:
            card_text = " ".join(
                t for texts in card["facts"].values() for t in texts
            )
            card_entities = extract_entities(card_text)
            card_tokens = set(_tokens(card_text))
            union = alert_tokens | card_tokens
            overlap = len(alert_tokens & card_tokens) / len(union) if union else 0.0
            entity_overlap = len(alert_entities & card_entities)
            service_match = bool(fp.get("service")) and card["meta"].get("service") == fp["service"]
            card["relevance"] = {
                "service_match": service_match,
                "token_overlap": round(overlap, 4),
                "entity_overlap": entity_overlap,
            }

        assembled["incidents"] = [
            c
            for c in assembled["incidents"]
            if c["relevance"]["service_match"]
            or c["relevance"]["entity_overlap"] >= 2
            or c["relevance"]["token_overlap"] >= 0.09
        ]
        assembled["incidents"].sort(
            key=lambda c: (c["relevance"]["service_match"], c["score"]), reverse=True
        )
        if not assembled["incidents"]:
            assembled["observations"] = []
            assembled["mental_models"] = []
            return False
        return True

    @staticmethod
    def render_memory_context(assembled: dict[str, Any], max_incidents: int = 4) -> str:
        blocks: list[str] = []
        for model in assembled["mental_models"][:2]:
            blocks.append(f"[CURATED PLAYBOOK] {model['text']}")
        for obs in assembled["observations"][:4]:
            blocks.append(
                f"[OBSERVATION · supported by {obs['proof_count']} incidents] {obs['text']}"
            )
        for card in assembled["incidents"][:max_incidents]:
            meta = card["meta"]
            lines = [
                f"[PAST INCIDENT {card['incident_id']}] service={meta.get('service','?')} "
                f"family={meta.get('failure_class','?')} severity={meta.get('severity','?')} "
                f"date={str(meta.get('opened_at',''))[:10]} mttr={meta.get('mttr_minutes','?')}min"
            ]
            for kind in ("alert", "alert_payload", "symptoms", "root_cause", "fix", "false_lead", "verification"):
                for text in card["facts"].get(kind, [])[:2]:
                    lines.append(f"  - {kind}: {text}")
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks) if blocks else "(memory returned nothing relevant)"

    # -- triage ------------------------------------------------------------- #

    def triage(self, alert_text: str, use_memory: bool = True) -> dict[str, Any]:
        started = datetime.now(timezone.utc)
        fp = fingerprint(alert_text)

        if not use_memory:
            brief = self._llm_brief(alert_text, memory_context="", use_memory=False)
            brief.update(
                {
                    "mode": "no-memory",
                    "memory_hits": [],
                    "memory_used": 0,
                    "fingerprint": fp,
                    "latency_ms": _ms_since(started),
                }
            )
            brief["spoken"] = speakable(brief)
            return brief

        fp, hits = self.recall_for_alert(alert_text)
        assembled = self.assemble(hits)
        self.enrich(assembled)
        grounded = self.score_relevance(fp, assembled)
        context = self.render_memory_context(assembled) if grounded else "(memory holds no incident resembling this alert)"
        brief = self._llm_brief(alert_text, memory_context=context, use_memory=True, assembled=assembled)
        brief.update(
            {
                "mode": "hindsight-memory",
                "memory_backend": self.store.mode,
                "memory_relevant": grounded,
                "memory_hits": [h.to_dict() for h in hits[:12]],
                "memory_used": len(hits),
                "recalled_incidents": [
                    {
                        "incident_id": c["incident_id"],
                        "score": round(c["score"], 2),
                        "service": c["meta"].get("service"),
                        "failure_class": c["meta"].get("failure_class"),
                        "opened_at": c["meta"].get("opened_at"),
                        "mttr_minutes": c["meta"].get("mttr_minutes"),
                    }
                    for c in assembled["incidents"][:6]
                ],
                "observations": assembled["observations"][:4],
                "mental_models": assembled["mental_models"][:2],
                "fingerprint": fp,
                "latency_ms": _ms_since(started),
            }
        )
        brief["recommendations"] = [
            r.to_dict()
            for r in build_recommendations(brief, fp.get("service", ""), self.decisions)
        ]
        brief["prior_decisions"] = self._recall_prior_decisions(fp, assembled)
        brief["spoken"] = speakable(brief)
        return brief

    def compare(self, alert_text: str) -> dict[str, Any]:
        """The demo centrepiece: identical alert, memory off vs memory on."""
        without = self.triage(alert_text, use_memory=False)
        with_memory = self.triage(alert_text, use_memory=True)
        return {
            "alert": alert_text,
            "without_memory": without,
            "with_memory": with_memory,
            "delta": {
                "incidents_cited": len(with_memory.get("similar_incidents", [])),
                "warnings_from_past_mistakes": len(with_memory.get("do_not_do", [])),
                "memories_recalled": with_memory.get("memory_used", 0),
                "mttr_estimate_minutes": with_memory.get("estimated_mttr_minutes"),
            },
        }

    # -- reasoning ---------------------------------------------------------- #

    def _llm_brief(
        self,
        alert_text: str,
        memory_context: str,
        use_memory: bool,
        assembled: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        system = SYSTEM_WITH_MEMORY if use_memory else SYSTEM_WITHOUT_MEMORY
        if use_memory:
            user = (
                f"LIVE ALERT:\n{alert_text}\n\n"
                f"RECALLED MEMORIES (from the team's incident history):\n{memory_context}\n\n"
                "Write the triage brief. Cite incident IDs in every supporting_incidents and "
                "source_incident field."
            )
        else:
            user = f"LIVE ALERT:\n{alert_text}\n\nWrite the triage brief."

        try:
            brief = llm.complete_json(system, user)
            brief["synthesis"] = f"llm:{_model_name()}"
            return _normalise_brief(brief)
        except llm.LLMUnavailable as exc:
            brief = (
                _synthesise_with_memory(alert_text, assembled or {})
                if use_memory
                else _synthesise_without_memory(alert_text)
            )
            brief["synthesis"] = "deterministic-fallback"
            brief["synthesis_note"] = f"LLM unavailable ({exc}); brief composed directly from memory."
            return _normalise_brief(brief)

    # -- write-back --------------------------------------------------------- #

    def record_outcome(
        self,
        *,
        incident_id: str,
        service: str,
        failure_class: str,
        alert_title: str,
        root_cause: str,
        fix: str,
        mttr_minutes: int,
        responder: str = "on-call",
        false_leads: str = "",
        severity: str = "SEV2",
        alert_payload: str = "",
    ) -> dict[str, Any]:
        """Close the loop: what actually happened becomes memory for next time."""
        incident = {
            "incident_id": incident_id,
            "opened_at": datetime.now(timezone.utc).isoformat(),
            "service": service,
            "severity": severity,
            "responder": responder,
            "failure_class": failure_class,
            "alert_title": alert_title,
            "alert_payload": alert_payload or alert_title,
            "symptoms": alert_title,
            "false_leads": false_leads or "None recorded.",
            "root_cause": root_cause,
            "fix": fix,
            "verification": "Recorded by the responder at resolution time.",
            "mttr_minutes": int(mttr_minutes),
            "customer_impact": "Recorded by the responder at resolution time.",
            "tags": [failure_class],
        }
        items = incident_to_memory_items(incident)
        self.store.retain(items)
        self.ledger.add(
            LedgerEntry(
                incident_id=incident_id,
                opened_at=incident["opened_at"],
                service=service,
                failure_class=failure_class,
                mttr_minutes=int(mttr_minutes),
                severity=severity,
                responder=responder,
                source="live",
            )
        )
        return {"retained": len(items), "incident_id": incident_id, **self.store.stats()}

    # -- the approval loop -------------------------------------------------- #

    def _recall_prior_decisions(
        self, fp: dict[str, Any], assembled: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """
        Surface what this team decided last time, not just what happened last time.

        Decisions live in the same memory bank as incidents, so this is a recall
        against Hindsight filtered to decision facts — the same retrieval path,
        a different kind of fact.
        """
        service = fp.get("service") or ""
        families = [
            c["meta"].get("failure_class")
            for c in assembled.get("incidents", [])[:3]
            if c["meta"].get("failure_class")
        ]
        if not service and not families:
            return []
        query = f"decision approved rejected recommendation {service} {' '.join(families[:2])}"

        # Long facts are split into sentences at retain time, so one decision can
        # come back as several fragments. Collapse them by recommendation, keeping
        # the fullest sentence — a judge should see one decision, not three shards.
        best: dict[str, dict[str, Any]] = {}
        for hit in self.store.recall(query, limit=12):
            if hit.metadata.get("kind") != "decision":
                continue
            key = f"{hit.metadata.get('recommendation_key')}::{hit.metadata.get('decision')}"
            current = best.get(key)
            if current and len(current["text"]) >= len(hit.text):
                continue
            best[key] = {
                "text": hit.text,
                "decision": hit.metadata.get("decision"),
                "outcome": hit.metadata.get("outcome"),
                "service": hit.metadata.get("service"),
                "action": hit.metadata.get("action"),
                "recommendation_key": hit.metadata.get("recommendation_key"),
                "decided_by": hit.metadata.get("decided_by"),
            }
        return list(best.values())[:4]

    def record_decision(
        self,
        *,
        recommendation_key: str,
        decision: str,
        action: str,
        service: str,
        failure_class: str = "",
        decided_by: str = "on-call",
        note: str = "",
        alert_title: str = "",
    ) -> dict[str, Any]:
        """
        A human ruled on a recommendation. Write it to the log AND to memory.

        The memory write is the point: next time a similar alert fires, the recall
        that surfaces past incidents also surfaces past decisions about them.
        """
        record = Decision(
            recommendation_key=recommendation_key,
            decision=decision,
            action=action,
            service=service,
            failure_class=failure_class,
            decided_by=decided_by,
            note=note,
        )
        self.decisions.add(record)

        text, metadata = decision_memory(record, alert_title)
        self.store.retain(
            [
                MemoryItem(
                    content=text,
                    context=f"decision on {service} ({failure_class})",
                    type=EXPERIENCE,
                    metadata=metadata,
                )
            ]
        )
        return {
            "recorded": record.to_dict(),
            "track_record": self.decisions.track_record(recommendation_key),
            "memory": text,
            "stats": self.decisions.stats(),
        }

    def record_decision_outcome(
        self, *, recommendation_key: str, outcome: str, note: str = ""
    ) -> dict[str, Any]:
        """Did the approved action actually work? This is the signal that matters."""
        updated = self.decisions.record_outcome(recommendation_key, outcome, note)
        if not updated:
            return {"updated": False, "reason": "no approved decision found for that recommendation"}

        text, metadata = decision_memory(updated)
        self.store.retain(
            [
                MemoryItem(
                    content=text,
                    context=f"decision outcome on {updated.service}",
                    type=EXPERIENCE,
                    metadata=metadata,
                )
            ]
        )
        return {
            "updated": True,
            "decision": updated.to_dict(),
            "track_record": self.decisions.track_record(recommendation_key),
            "memory": text,
            "stats": self.decisions.stats(),
        }

    # -- evaluation --------------------------------------------------------- #

    def learning_curve(self) -> dict[str, Any]:
        """
        Replay the incident history chronologically against a *fresh* memory,
        scoring every incident using only the memories that existed before it.

        Two metrics, because "the agent gets better" needs to survive scrutiny:

          coverage   — of all incidents seen so far, how many did memory correctly
                       brief? Starts at 0 (nothing to remember) and climbs as
                       failure families repeat. This is the learning curve.
          precision  — when a precedent DID exist, did recall put it in the top 3?
                       And when no precedent existed, did the agent correctly
                       decline to pattern-match? False positives are counted.
        """
        from .memory import LocalMemoryEngine

        engine = LocalMemoryEngine(None)
        scratch = MemoryStore.__new__(MemoryStore)
        scratch.backend = engine
        scratch.mode = "local-eval"
        scratch.settings = self.store.settings
        evaluator = StarkAgent(scratch, self.ledger)

        points: list[dict[str, Any]] = []
        seen_families: set[str] = set()
        true_pos = false_neg = true_neg = false_pos = 0
        same_service_fp = [0]

        for index, incident in enumerate(sorted(INCIDENTS, key=lambda i: i["opened_at"]), start=1):
            family = incident["failure_class"]
            alert = (
                f"ALERT: {incident['alert_title']}\n"
                f"service={incident['service']}\n{incident['alert_payload']}"
            )
            had_prior = family in seen_families

            fp, recalled = evaluator.recall_for_alert(alert, limit=14)
            assembled = evaluator.assemble(recalled)
            evaluator.enrich(assembled, top_n=2)
            relevant = evaluator.score_relevance(fp, assembled)
            top3 = [c["meta"].get("failure_class") for c in assembled["incidents"][:3]]
            surfaced = relevant and family in top3

            if had_prior:
                if surfaced:
                    true_pos += 1
                    outcome = "correctly recalled precedent"
                else:
                    false_neg += 1
                    outcome = "missed an existing precedent"
            else:
                if relevant and top3 and family not in top3:
                    false_pos += 1
                    same_service = any(
                        c["meta"].get("service") == incident["service"]
                        for c in assembled["incidents"][:3]
                    )
                    outcome = (
                        "matched a different failure family on the same service"
                        if same_service
                        else "wrongly matched an unrelated incident"
                    )
                    same_service_fp[0] += int(same_service)
                else:
                    true_neg += 1
                    outcome = "correctly treated as novel"

            correct = true_pos + true_neg
            points.append(
                {
                    "n": index,
                    "incident_id": incident["incident_id"],
                    "opened_at": incident["opened_at"][:10],
                    "failure_class": family,
                    "service": incident["service"],
                    "mttr_minutes": incident["mttr_minutes"],
                    "memory_size": scratch.stats().get("facts", 0),
                    "had_prior_family_incident": had_prior,
                    "precedent_surfaced": surfaced,
                    "outcome": outcome,
                    "coverage": round(true_pos / index, 3),
                    "running_accuracy": round(correct / index, 3),
                }
            )

            scratch.retain(incident_to_memory_items(incident))
            seen_families.add(family)

        total = len(points)
        with_precedent = true_pos + false_neg
        return {
            "provenance": {
                "data_source": "synthetic corpus (app/seed_data.py), 21 incidents over 12 months",
                "is_synthetic": True,
                "method": (
                    "Chronological held-out replay. The corpus is sorted by opened_at and fed to a "
                    "fresh, empty memory one incident at a time. Each incident is scored BEFORE it "
                    "is retained, so the agent only ever sees memories that existed strictly "
                    "earlier. No lookahead, no train/test leakage."
                ),
                "definitions": {
                    "precedent_surfaced": (
                        "The grounding gate passed AND an incident of the same failure_class "
                        "appears in the top 3 recalled incidents."
                    ),
                    "true_positive": "A precedent existed in memory and was surfaced.",
                    "false_negative": "A precedent existed in memory and was missed.",
                    "true_negative": "No precedent existed and the agent correctly stayed quiet.",
                    "false_positive": "No precedent existed but the agent surfaced another family.",
                },
                "formulas": {
                    "coverage": f"true_positive / total_incidents = {true_pos} / {total}",
                    "recall_when_precedent_exists": (
                        f"true_positive / (true_positive + false_negative) = "
                        f"{true_pos} / {with_precedent}"
                    ),
                    "false_positive_rate_on_novel": (
                        f"false_positive / (false_positive + true_negative) = "
                        f"{false_pos} / {false_pos + true_neg}"
                    ),
                },
                "caveat": (
                    "These are replay-evaluation results on a synthetic corpus written for this "
                    "project. They measure retrieval behaviour, not production incident outcomes."
                ),
            },
            "points": points,
            "total_incidents": total,
            "incidents_with_precedent": with_precedent,
            "coverage": round(true_pos / total, 3) if total else None,
            "recall_when_precedent_exists": round(true_pos / with_precedent, 3) if with_precedent else None,
            "false_positive_rate_on_novel": round(false_pos / (false_pos + true_neg), 3)
            if (false_pos + true_neg)
            else None,
            "confusion": {
                "true_positive": true_pos,
                "false_negative": false_neg,
                "true_negative": true_neg,
                "false_positive": false_pos,
                "false_positive_same_service": same_service_fp[0],
            },
            "mttr": self.ledger.mttr_summary(),
            "families": self.ledger.by_family(),
        }


# --------------------------------------------------------------------------- #
# Deterministic fallback synthesis (used when no LLM key is present)
# --------------------------------------------------------------------------- #


def _synthesise_with_memory(alert_text: str, assembled: dict[str, Any]) -> dict[str, Any]:
    cards = assembled.get("incidents", [])[:4]
    observations = assembled.get("observations", [])[:3]
    models = assembled.get("mental_models", [])[:2]

    if not cards:
        return {
            "verdict": "No incident in memory resembles this alert. Treat it as novel.",
            "confidence": "low",
            "memory_grounded": False,
            "similar_incidents": [],
            "likely_root_causes": [],
            "first_actions": [
                {
                    "step": "Establish blast radius and start a timeline before changing anything.",
                    "rationale": "Memory holds no precedent, so avoid speculative remediation.",
                    "source_incident": "",
                }
            ],
            "do_not_do": [],
            "estimated_mttr_minutes": None,
            "open_questions": ["Is this a genuinely new failure mode, or an existing one on a new service?"],
        }

    top = cards[0]
    family = top["meta"].get("failure_class", "unknown")
    service = top["meta"].get("service", "the service")
    same_family = [c for c in cards if c["meta"].get("failure_class") == family]
    mttrs = [c["meta"].get("mttr_minutes") for c in same_family if isinstance(c["meta"].get("mttr_minutes"), int)]

    similar = [
        {
            "incident_id": c["incident_id"],
            "why": f"Same failure family ({c['meta'].get('failure_class')}) on {c['meta'].get('service')}.",
            "similarity": "high" if c["meta"].get("failure_class") == family else "medium",
        }
        for c in cards
    ]

    root_causes = []
    for card in same_family[:3]:
        for text in card["facts"].get("root_cause", [])[:1]:
            root_causes.append(
                {
                    "cause": _strip_prefix(text),
                    "supporting_incidents": [card["incident_id"]],
                    "confidence": "high" if len(same_family) > 1 else "medium",
                }
            )

    first_actions = []
    for model in models:
        first_actions.append(
            {
                "step": model["text"].split(". ")[0] + ".",
                "rationale": "Curated team playbook stored as a mental model.",
                "source_incident": "",
            }
        )
    for card in same_family[:3]:
        fix = card["meta"].get("fix")
        if fix:
            first_actions.append(
                {
                    "step": fix,
                    "rationale": f"This resolved {card['incident_id']} in {card['meta'].get('mttr_minutes','?')} minutes.",
                    "source_incident": card["incident_id"],
                }
            )

    do_not_do = []
    for card in cards:
        for text in card["facts"].get("false_lead", [])[:1]:
            do_not_do.append(
                {
                    "action": _strip_prefix(text),
                    "reason": f"This cost the team time during {card['incident_id']}.",
                    "source_incident": card["incident_id"],
                }
            )

    times = "once" if len(same_family) == 1 else f"{len(same_family)} times"
    verdict_bits = [
        f"This matches the {family} family, which this team has resolved {times} on {service}."
    ]
    top_fix = next((o.get("recommended_fix") for o in observations if o.get("recommended_fix")), None)
    if top_fix:
        verdict_bits.append(f"The resolution that has worked before: {top_fix}")

    return {
        "verdict": " ".join(verdict_bits),
        "confidence": "high" if len(same_family) >= 2 else "medium",
        "memory_grounded": True,
        "similar_incidents": similar,
        "likely_root_causes": root_causes,
        "first_actions": first_actions[:5],
        "do_not_do": do_not_do[:4],
        "estimated_mttr_minutes": int(sorted(mttrs)[len(mttrs) // 2]) if mttrs else None,
        "open_questions": [
            f"Does the current signature match {top['incident_id']} exactly, or only superficially?",
        ],
    }


def _synthesise_without_memory(alert_text: str) -> dict[str, Any]:
    fp = fingerprint(alert_text)
    service = fp["service"] or "the affected service"
    return {
        "verdict": f"An alert is firing on {service}. Without incident history I can only "
        "suggest a generic triage sequence.",
        "confidence": "low",
        "memory_grounded": False,
        "similar_incidents": [],
        "likely_root_causes": [
            {"cause": "Recent deployment or configuration change", "supporting_incidents": [], "confidence": "low"},
            {"cause": "Resource saturation (CPU, memory, connections, or queue depth)", "supporting_incidents": [], "confidence": "low"},
            {"cause": "Upstream or downstream dependency degradation", "supporting_incidents": [], "confidence": "low"},
        ],
        "first_actions": [
            {"step": "Check recent deploys and roll back if one correlates in time.", "rationale": "Standard first move.", "source_incident": ""},
            {"step": "Inspect dashboards for saturation signals on the affected service.", "rationale": "Standard first move.", "source_incident": ""},
            {"step": "Scale the service horizontally to buy headroom.", "rationale": "Standard first move.", "source_incident": ""},
            {"step": "Page the service owner for context.", "rationale": "No historical context available.", "source_incident": ""},
        ],
        "do_not_do": [],
        "estimated_mttr_minutes": None,
        "open_questions": [
            "Has this happened before?",
            "What resolved it last time?",
            "Which mitigations have already been tried and failed?",
        ],
    }


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

_ID_RE = re.compile(r"\bINC-\d{3,6}\b")

#: Unicode dash characters an LLM will happily emit where a human typed a hyphen:
#: non-breaking hyphen, figure dash, en/em dash, minus sign. They look identical
#: in a browser and break every exact-match comparison downstream — most
#: importantly `INC-1131`, which is how incidents are matched back to memory.
_DASHES = re.compile(r"[\u2010-\u2015\u2212]")


def normalise_dashes(value: Any) -> Any:
    """Recursively map exotic dashes to ASCII hyphens through nested structures."""
    if isinstance(value, str):
        return _DASHES.sub("-", value)
    if isinstance(value, list):
        return [normalise_dashes(v) for v in value]
    if isinstance(value, dict):
        return {k: normalise_dashes(v) for k, v in value.items()}
    return value


def _tokens(text: str) -> list[str]:
    from .memory import tokenize

    return tokenize(text)


def _incident_id_in(text: str) -> str:
    match = _ID_RE.search(text or "")
    return match.group(0) if match else ""


def _strip_prefix(text: str) -> str:
    return re.sub(r"^INC-\d+[^:]*:\s*", "", text or "").strip()


def _ms_since(started: datetime) -> int:
    return int((datetime.now(timezone.utc) - started).total_seconds() * 1000)


def _model_name() -> str:
    from .config import settings

    return settings.groq_model


_BRIEF_KEYS = {
    "verdict": "",
    "confidence": "low",
    "memory_grounded": False,
    "similar_incidents": list,
    "likely_root_causes": list,
    "first_actions": list,
    "do_not_do": list,
    "estimated_mttr_minutes": None,
    "open_questions": list,
}


def _normalise_brief(brief: dict[str, Any]) -> dict[str, Any]:
    """Models drift from the schema; make the response safe for the UI."""
    out = normalise_dashes(dict(brief or {}))
    for key, default in _BRIEF_KEYS.items():
        if key not in out or out[key] is None and default is list:
            out[key] = default() if default is list else default
        if default is list and not isinstance(out.get(key), list):
            out[key] = [out[key]] if out.get(key) else []
    mttr = out.get("estimated_mttr_minutes")
    if isinstance(mttr, str):
        digits = re.findall(r"\d+", mttr)
        out["estimated_mttr_minutes"] = int(digits[0]) if digits else None
    return out
