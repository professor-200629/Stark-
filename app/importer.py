"""
Bring your own incidents.

The corpus shipped with STARK is synthetic and labelled as such everywhere, which
is honest but limits how far a demo can go. This module closes that gap: paste a
real postmortem and STARK turns it into memory.

Two extraction paths:

  headings   deterministic. Postmortems are written to a template — "Root cause:",
             "## Resolution", "What went wrong", "Timeline" — so a heading parser
             gets most documents without a model, without a key, and without
             sending anyone's incident data anywhere.
  llm        used only when a key is configured and the heading parser came back
             thin. Better at prose that ignores the template.

Whatever the path, the result is shown to the user for confirmation before it is
retained. Nothing silently enters memory: a wrong root cause in the bank is worse
than no root cause, because it will be cited with confidence six months from now.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from . import llm

#: Heading synonyms seen across real postmortem templates.
FIELD_PATTERNS: dict[str, list[str]] = {
    "root_cause": [
        "root cause", "root-cause", "cause", "what happened", "what went wrong",
        "why it happened", "why this happened", "analysis", "contributing factors",
        "diagnosis", "trigger",
    ],
    "fix": [
        "resolution", "fix", "remediation", "what fixed it", "mitigation",
        "how it was resolved", "how we fixed it", "corrective action", "action taken",
        "recovery",
    ],
    "false_leads": [
        "false lead", "false leads", "dead end", "dead ends", "what did not work",
        "what didn't work", "red herring", "red herrings", "wasted time",
        "things we tried", "unsuccessful",
    ],
    "symptoms": ["symptoms", "impact", "detection", "what we saw", "observed"],
    "alert_title": ["summary", "title", "incident", "overview", "tl;dr", "tldr"],
    "verification": ["verification", "how we verified", "confirmation", "validation"],
    "customer_impact": ["customer impact", "user impact", "blast radius", "scope"],
}

#: A heading must be *marked* as one. Matching any short line of prose meant a
#: sentence like "we blamed the network" was read as a heading, which silently
#: discarded the content underneath the real heading above it.
_HEADING_MARKDOWN = re.compile(r"^\s{0,3}#{1,6}\s*(.+?)\s*:?\s*$")
_HEADING_BOLD = re.compile(r"^\s{0,3}(?:\*\*|__)\s*(.+?)\s*(?:\*\*|__)\s*:?\s*$")
_HEADING_COLON = re.compile(r"^\s{0,3}([A-Za-z][A-Za-z '’\-/]{2,48})\s*:\s*$")


def _heading_text(line: str) -> str | None:
    """Return the heading text if this line is marked as a heading, else None."""
    for pattern in (_HEADING_MARKDOWN, _HEADING_BOLD, _HEADING_COLON):
        match = pattern.match(line)
        if match:
            return match.group(1)
    return None
_INLINE = re.compile(r"^\s*(?:[-*]\s*)?(?:\*\*|__)?([A-Za-z][A-Za-z '’\-/]{2,48}?)(?:\*\*|__)?\s*:\s*(.+)$")

_ID = re.compile(r"\b((?:INC|INCIDENT|SEV|OPS|IR)[-_ ]?\d{2,8})\b", re.I)
_SERVICE = re.compile(
    r"\b([a-z][a-z0-9]*(?:-[a-z0-9]+)*-(?:api|svc|service|worker|gateway|web|scorer|db|cache|queue))\b",
    re.I,
)
_MINUTES = re.compile(
    r"(?:mttr|time to resolve|resolved in|duration|took)\D{0,18}(\d{1,4})\s*(min|minute|m\b|hour|hr|h\b)",
    re.I,
)
_SEVERITY = re.compile(r"\b(SEV[-\s]?[0-4]|P[0-4]|critical|major|minor)\b", re.I)


@dataclass
class ParsedIncident:
    incident_id: str = ""
    service: str = ""
    failure_class: str = ""
    alert_title: str = ""
    root_cause: str = ""
    fix: str = ""
    false_leads: str = ""
    symptoms: str = ""
    verification: str = ""
    customer_impact: str = ""
    severity: str = "SEV3"
    mttr_minutes: int = 0
    responder: str = "imported"
    opened_at: str = ""
    method: str = "headings"
    confidence: str = "low"
    missing: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


REQUIRED = ("service", "root_cause", "fix")
USEFUL = ("alert_title", "false_leads", "symptoms", "mttr_minutes", "failure_class")


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip(" \t\n-*#>")).strip()


def _match_field(heading: str) -> str | None:
    h = heading.strip().lower().rstrip(":")
    for field_name, synonyms in FIELD_PATTERNS.items():
        for synonym in synonyms:
            if h == synonym or h.startswith(synonym) or synonym in h:
                return field_name
    return None


def parse_headings(text: str) -> dict[str, str]:
    """
    Walk the document collecting text under each recognised heading.

    Handles both block headings (`## Root cause` followed by paragraphs) and
    inline ones (`Root cause: the pool was exhausted`), because postmortem
    templates use both and often mix them in the same document.
    """
    sections: dict[str, list[str]] = {}
    current: str | None = None

    for raw_line in (text or "").splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            continue

        inline = _INLINE.match(line)
        if inline:
            field_name = _match_field(inline.group(1))
            if field_name:
                sections.setdefault(field_name, []).append(inline.group(2).strip())
                current = field_name
                continue

        heading = _heading_text(line)
        if heading is not None and len(heading) < 60:
            field_name = _match_field(heading)
            current = field_name
            if field_name:
                sections.setdefault(field_name, [])
            continue

        if current:
            sections[current].append(line.strip())

    return {k: _clean(" ".join(v)) for k, v in sections.items() if _clean(" ".join(v))}


def _guess_failure_class(parsed: ParsedIncident) -> str:
    """A slug from the root cause, so imported incidents can form families."""
    source = parsed.root_cause or parsed.alert_title
    words = [
        w
        for w in re.findall(r"[a-z]{4,}", source.lower())
        if w not in {"that", "this", "with", "from", "were", "which", "when", "because", "after"}
    ]
    return "-".join(words[:3]) or "imported-incident"


def _extract_scalars(text: str, parsed: ParsedIncident) -> None:
    if (found := _ID.search(text)) and not parsed.incident_id:
        parsed.incident_id = re.sub(r"[-_ ]", "-", found.group(1).upper())
    if (found := _SERVICE.search(text)) and not parsed.service:
        parsed.service = found.group(1).lower()
    if found := _SEVERITY.search(text):
        raw = found.group(1).upper().replace(" ", "").replace("-", "")
        parsed.severity = raw if raw.startswith("SEV") else {"P0": "SEV1", "P1": "SEV1", "P2": "SEV2"}.get(raw, "SEV3")
    if found := _MINUTES.search(text):
        value = int(found.group(1))
        parsed.mttr_minutes = value * 60 if found.group(2).lower().startswith(("hour", "hr", "h")) else value


def parse_postmortem(text: str, use_llm: bool = True) -> ParsedIncident:
    """Extract a structured incident from free-form postmortem text."""
    parsed = ParsedIncident(method="headings")
    sections = parse_headings(text)
    for key, value in sections.items():
        if hasattr(parsed, key):
            setattr(parsed, key, value)
    _extract_scalars(text, parsed)

    thin = sum(1 for f in REQUIRED if getattr(parsed, f)) < 2
    if thin and use_llm and llm.available():
        try:
            parsed = _parse_with_llm(text, parsed)
        except llm.LLMUnavailable as exc:
            parsed.warnings.append(f"LLM extraction unavailable ({exc}); used the heading parser.")

    if not parsed.alert_title:
        first = next((ln.strip(" #*") for ln in (text or "").splitlines() if ln.strip()), "")
        parsed.alert_title = _clean(first)[:140]
    if not parsed.failure_class:
        parsed.failure_class = _guess_failure_class(parsed)
    if not parsed.incident_id:
        parsed.incident_id = "IMP-" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    if not parsed.opened_at:
        parsed.opened_at = datetime.now(timezone.utc).isoformat()

    parsed.missing = [f for f in REQUIRED + USEFUL if not getattr(parsed, f)]
    have_required = sum(1 for f in REQUIRED if getattr(parsed, f))
    parsed.confidence = (
        "high" if have_required == len(REQUIRED) and parsed.false_leads
        else "medium" if have_required == len(REQUIRED)
        else "low"
    )
    if not parsed.false_leads:
        parsed.warnings.append(
            "No dead end found. That field is the most valuable thing in a postmortem — "
            "add what wasted time before importing, if you can."
        )
    if have_required < len(REQUIRED):
        parsed.warnings.append(
            f"Missing {', '.join(f for f in REQUIRED if not getattr(parsed, f))}. "
            "Fill these in before retaining, or the incident will be recalled without them."
        )
    return parsed


_LLM_SYSTEM = """You extract structured incident records from postmortem documents.

Return ONLY JSON with these keys, using "" when the document does not say:
{"incident_id":"","service":"","failure_class":"","alert_title":"","root_cause":"",
 "fix":"","false_leads":"","symptoms":"","verification":"","customer_impact":"",
 "severity":"","mttr_minutes":0,"responder":""}

Rules:
- Copy from the document. Never invent a root cause, a fix, or a metric.
- failure_class is a short lowercase hyphenated slug naming the mechanism,
  e.g. "db-connection-pool-exhaustion".
- false_leads is anything the responders tried that did not work or wasted time.
- mttr_minutes is an integer number of minutes, 0 if not stated."""


def _parse_with_llm(text: str, fallback: ParsedIncident) -> ParsedIncident:
    data = llm.complete_json(_LLM_SYSTEM, f"POSTMORTEM:\n{text[:12000]}")
    parsed = ParsedIncident(method="llm")
    for key, value in data.items():
        if hasattr(parsed, key) and value not in (None, ""):
            setattr(parsed, key, value)
    try:
        parsed.mttr_minutes = int(parsed.mttr_minutes or 0)
    except (TypeError, ValueError):
        parsed.mttr_minutes = 0
    # Anything the model left blank falls back to the deterministic parse.
    for key in ("incident_id", "service", "root_cause", "fix", "false_leads",
                "symptoms", "alert_title", "severity"):
        if not getattr(parsed, key) and getattr(fallback, key):
            setattr(parsed, key, getattr(fallback, key))
    if not parsed.mttr_minutes:
        parsed.mttr_minutes = fallback.mttr_minutes
    return parsed


def to_incident(parsed: ParsedIncident) -> dict[str, Any]:
    """Shape a parsed record the way the rest of STARK expects an incident."""
    return {
        "incident_id": parsed.incident_id,
        "opened_at": parsed.opened_at or datetime.now(timezone.utc).isoformat(),
        "service": parsed.service or "unknown-service",
        "severity": parsed.severity or "SEV3",
        "responder": parsed.responder or "imported",
        "failure_class": parsed.failure_class,
        "alert_title": parsed.alert_title,
        "alert_payload": parsed.symptoms or parsed.alert_title,
        "symptoms": parsed.symptoms or parsed.alert_title,
        "false_leads": parsed.false_leads or "None recorded.",
        "root_cause": parsed.root_cause,
        "fix": parsed.fix,
        "verification": parsed.verification or "Imported from a postmortem document.",
        "mttr_minutes": int(parsed.mttr_minutes or 0),
        "customer_impact": parsed.customer_impact or "Not stated in the source document.",
        "tags": ["imported", parsed.failure_class],
    }


SAMPLE_POSTMORTEM = """# Incident INC-2291 — checkout-api returning 502s during EU peak

**Severity:** SEV1
**Duration:** resolved in 47 minutes

## Summary
checkout-api began returning 502 for roughly 30% of requests at 19:12 UTC. Error
rate peaked at 34% before mitigation.

## What we saw
Envoy sidecar logs filled with `upstream connect error or disconnect/reset before headers`.
Pod CPU and memory were both normal. The gateway health check kept passing, which
is why the on-call alert fired eight minutes late.

## Things we tried
We spent the first 20 minutes convinced this was a bad deploy and rolled back to the
previous image. That changed nothing — the rollback was a waste of time and the
image was fine. We also restarted the ingress controller, which briefly made the
error rate worse.

## Root cause
The Envoy sidecar `max_requests_per_connection` was left at the default while the
upstream service had recently enabled HTTP/2 keepalive. Connections were being
recycled mid-stream under peak concurrency, producing resets that surfaced as 502s.

## Resolution
Set `max_requests_per_connection: 0` on the checkout-api sidecar and redeploy the
mesh config. Error rate returned to baseline within 3 minutes.

## Verification
Error rate back to 0.2% and held flat through the following peak window.

## Customer impact
Approximately 14,000 failed checkout attempts over 47 minutes.
"""
