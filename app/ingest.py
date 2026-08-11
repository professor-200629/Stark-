"""
Webhook ingestion — the incident comes to STARK, not the other way round.

A monitoring system does not hand you a tidy paragraph. It POSTs a nested JSON
document whose shape depends entirely on which vendor is paging you, and the
useful signal — service name, the metric that broke, the log line — is scattered
across labels, annotations, tags and free text.

This module normalises the four shapes that cover most real deployments into the
plain alert text STARK's fingerprinter already understands, so the triage path is
identical whether a human pasted the alert or Alertmanager fired it at 03:00.

Parsing is deliberately defensive: an unrecognised payload is flattened rather
than rejected, because a webhook endpoint that 400s at 3am is worse than one that
does something imperfect with an unfamiliar body.
"""

from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ALERTMANAGER = "prometheus-alertmanager"
DATADOG = "datadog"
GRAFANA = "grafana"
GENERIC = "generic"
UNKNOWN = "unknown"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class IncomingAlert:
    """A normalised alert, whatever shape it arrived in."""

    alert_text: str
    source: str
    service: str = ""
    title: str = ""
    severity: str = ""
    fired_at: str = field(default_factory=_now)
    received_at: str = field(default_factory=_now)
    external_id: str = ""
    raw_keys: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------- #
# Format detection and parsing
# --------------------------------------------------------------------------- #


def detect_format(payload: dict[str, Any]) -> str:
    if isinstance(payload.get("alerts"), list) and "receiver" in payload:
        return ALERTMANAGER
    if isinstance(payload.get("alerts"), list):
        return ALERTMANAGER
    if "alert_type" in payload or "event_type" in payload or "org" in payload:
        return DATADOG
    if "ruleName" in payload or "evalMatches" in payload or "dashboardId" in payload:
        return GRAFANA
    if {"service", "title"} & set(payload):
        return GENERIC
    return UNKNOWN


def _lines(*parts: Any) -> str:
    return "\n".join(str(p).strip() for p in parts if p and str(p).strip())


def _parse_alertmanager(payload: dict[str, Any]) -> IncomingAlert:
    alerts = payload.get("alerts") or [{}]
    first = alerts[0] if isinstance(alerts[0], dict) else {}
    labels = first.get("labels") or {}
    annotations = first.get("annotations") or {}

    name = labels.get("alertname") or payload.get("groupLabels", {}).get("alertname", "")
    service = labels.get("service") or labels.get("job") or labels.get("app") or ""
    severity = labels.get("severity", "")

    label_line = " ".join(
        f"{k}={v}" for k, v in labels.items() if k not in {"alertname", "severity"}
    )
    text = _lines(
        f"ALERT: {name}" if name else "",
        f"service={service} severity={severity}" if service or severity else "",
        label_line,
        annotations.get("summary"),
        annotations.get("description"),
        # Runbook log lines are usually stuffed into an annotation; keep them,
        # they carry the error signature the fingerprinter actually keys on.
        *[f"{k}: {v}" for k, v in annotations.items() if k not in {"summary", "description"}],
        f"firing since {first.get('startsAt', '')}" if first.get("startsAt") else "",
    )
    return IncomingAlert(
        alert_text=text,
        source=ALERTMANAGER,
        service=service,
        title=name or "Alertmanager alert",
        severity=severity,
        fired_at=first.get("startsAt") or _now(),
        external_id=first.get("fingerprint", ""),
        raw_keys=sorted(payload),
    )


def _parse_datadog(payload: dict[str, Any]) -> IncomingAlert:
    tags = payload.get("tags") or []
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]
    service = next(
        (t.split(":", 1)[1] for t in tags if isinstance(t, str) and t.startswith("service:")),
        payload.get("service", ""),
    )
    title = payload.get("title") or payload.get("event_title") or "Datadog monitor"
    text = _lines(
        f"ALERT: {title}",
        f"service={service}" if service else "",
        f"severity={payload.get('alert_type', '')}" if payload.get("alert_type") else "",
        " ".join(str(t) for t in tags),
        payload.get("body") or payload.get("text") or payload.get("message"),
        f"metric={payload.get('metric', '')} value={payload.get('value', '')}"
        if payload.get("metric")
        else "",
    )
    return IncomingAlert(
        alert_text=text,
        source=DATADOG,
        service=service,
        title=title,
        severity=str(payload.get("alert_type", "")),
        external_id=str(payload.get("id", "")),
        raw_keys=sorted(payload),
    )


def _parse_grafana(payload: dict[str, Any]) -> IncomingAlert:
    title = payload.get("title") or payload.get("ruleName") or "Grafana alert"
    tags = payload.get("tags") or {}
    service = tags.get("service", "") if isinstance(tags, dict) else ""
    matches = payload.get("evalMatches") or []
    metric_lines = [
        f"{m.get('metric', 'metric')}={m.get('value', '')}" for m in matches if isinstance(m, dict)
    ]
    text = _lines(
        f"ALERT: {title}",
        f"service={service}" if service else "",
        f"state={payload.get('state', '')}" if payload.get("state") else "",
        payload.get("message"),
        *metric_lines,
    )
    return IncomingAlert(
        alert_text=text,
        source=GRAFANA,
        service=service,
        title=title,
        severity=str(payload.get("state", "")),
        external_id=str(payload.get("ruleId", "")),
        raw_keys=sorted(payload),
    )


def _parse_generic(payload: dict[str, Any]) -> IncomingAlert:
    service = str(payload.get("service", ""))
    title = str(payload.get("title") or payload.get("alert") or "Alert")
    text = _lines(
        f"ALERT: {title}",
        f"service={service}" if service else "",
        f"severity={payload.get('severity', '')}" if payload.get("severity") else "",
        payload.get("description") or payload.get("message") or payload.get("body"),
        payload.get("logs") or payload.get("log"),
    )
    return IncomingAlert(
        alert_text=text,
        source=GENERIC,
        service=service,
        title=title,
        severity=str(payload.get("severity", "")),
        external_id=str(payload.get("id", "")),
        raw_keys=sorted(payload),
    )


def _parse_unknown(payload: dict[str, Any]) -> IncomingAlert:
    """
    Last resort: flatten whatever arrived into key=value lines.

    Rejecting an unfamiliar payload would mean a page goes unanswered because a
    vendor changed a field name, so this keeps everything and lets the
    fingerprinter find what it can.
    """
    flat: list[str] = []

    def walk(node: Any, prefix: str = "") -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, f"{prefix}.{k}" if prefix else str(k))
        elif isinstance(node, list):
            for i, v in enumerate(node[:5]):
                walk(v, f"{prefix}[{i}]")
        elif node not in (None, "", []):
            flat.append(f"{prefix}={node}")

    walk(payload)
    # A body with nothing extractable in it is not an imperfect alert, it is not
    # an alert. Returning a header-only string here would fill the inbox with
    # entries carrying no signal, so leave the text empty and let the endpoint
    # reject it.
    return IncomingAlert(
        alert_text=_lines("ALERT: unrecognised webhook payload", *flat[:40]) if flat else "",
        source=UNKNOWN,
        title="Unrecognised payload" if flat else "Empty payload",
        raw_keys=sorted(payload),
    )


_PARSERS = {
    ALERTMANAGER: _parse_alertmanager,
    DATADOG: _parse_datadog,
    GRAFANA: _parse_grafana,
    GENERIC: _parse_generic,
    UNKNOWN: _parse_unknown,
}


def normalise(payload: dict[str, Any]) -> IncomingAlert:
    """Turn any supported monitoring payload into alert text STARK can triage."""
    if not isinstance(payload, dict):
        return _parse_unknown({"payload": payload})
    fmt = detect_format(payload)
    try:
        return _PARSERS[fmt](payload)
    except Exception:  # a malformed body must still produce something triageable
        return _parse_unknown(payload)


# --------------------------------------------------------------------------- #
# Inbox
# --------------------------------------------------------------------------- #


@dataclass
class InboxEntry:
    received_at: str
    alert: dict[str, Any]
    summary: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Inbox:
    """Recent webhook deliveries and what STARK made of each one."""

    def __init__(self, path: Path | None = None, limit: int = 50) -> None:
        self._lock = threading.RLock()
        self.path = path
        self.limit = limit
        self.entries: list[InboxEntry] = []
        if path and path.exists():
            self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8") or "[]")
            self.entries = [InboxEntry(**e) for e in raw]
        except Exception:
            self.entries = []

    def save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps([e.to_dict() for e in self.entries], indent=2), encoding="utf-8"
        )

    def add(self, alert: IncomingAlert, summary: dict[str, Any]) -> InboxEntry:
        entry = InboxEntry(received_at=_now(), alert=alert.to_dict(), summary=summary)
        with self._lock:
            self.entries.append(entry)
            self.entries = self.entries[-self.limit :]
            self.save()
        return entry

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            return [e.to_dict() for e in reversed(self.entries[-limit:])]

    def stats(self) -> dict[str, Any]:
        with self._lock:
            total = len(self.entries)
            grounded = sum(1 for e in self.entries if e.summary.get("memory_grounded"))
            sources: dict[str, int] = {}
            for e in self.entries:
                src = e.alert.get("source", UNKNOWN)
                sources[src] = sources.get(src, 0) + 1
        return {
            "received": total,
            "grounded": grounded,
            "novel": total - grounded,
            "by_source": sources,
        }

    def reset(self) -> None:
        with self._lock:
            self.entries = []
            if self.path and self.path.exists():
                try:
                    self.path.unlink()
                except OSError:
                    self.path.write_text("[]", encoding="utf-8")


# --------------------------------------------------------------------------- #
# Demo payloads — real vendor shapes, so the demo is not a straw man
# --------------------------------------------------------------------------- #

SAMPLE_PAYLOADS: dict[str, dict[str, Any]] = {
    "alertmanager": {
        "receiver": "stark-webhook",
        "status": "firing",
        "groupLabels": {"alertname": "PaymentsApiLatencyHigh"},
        "alerts": [
            {
                "status": "firing",
                "fingerprint": "9f2c4a1b77e3",
                "startsAt": "2026-08-11T02:14:07.442Z",
                "labels": {
                    "alertname": "PaymentsApiLatencyHigh",
                    "service": "payments-api",
                    "severity": "critical",
                    "env": "prod",
                    "cluster": "eks-prod-1",
                },
                "annotations": {
                    "summary": "p99=3,640ms (threshold 800ms) sustained 7m",
                    "description": "HTTP 503 rate 3.4% on POST /v1/charges",
                    "log": "HikariPool-1 - Connection is not available, request timed out after 30000ms",
                    "pgbouncer": "cl_waiting=298 avg_wait_time=7,400,000us",
                    "runbook_url": "https://runbooks.internal/payments-api/latency",
                },
            }
        ],
    },
    "datadog": {
        "id": "8827361",
        "title": "KafkaConsumerLagCritical on ledger-worker",
        "alert_type": "error",
        "tags": ["service:ledger-worker", "env:prod", "team:payments"],
        "metric": "kafka.consumer_lag",
        "value": "402880",
        "body": (
            "group=ledger-worker topic=billing.settlements lag=402,880\n"
            "log: SerializationException: Unknown magic byte!\n"
            "no rebalance events in the last 30 minutes\n"
            "consumer poll returning 0 records"
        ),
    },
    "grafana": {
        "ruleId": 41,
        "ruleName": "AuthGateway401Spike",
        "state": "alerting",
        "tags": {"service": "auth-gateway", "env": "prod"},
        "message": (
            "401 rate = 24% on /oauth/introspect\n"
            "log: JWSVerificationException: no matching key found for kid=nw-2026-08-a\n"
            "signing key rotated 40 minutes ago"
        ),
        "evalMatches": [{"metric": "http_401_ratio", "value": 0.24}],
    },
    "generic": {
        "service": "webhook-dispatcher",
        "title": "WebhookDeliverySuccessRateLow",
        "severity": "SEV2",
        "description": "delivery success rate 61% (threshold 99%), affects 40% of merchant endpoints",
        "logs": "x509: certificate signed by unknown authority for merchant endpoint",
    },
}
