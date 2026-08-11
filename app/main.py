"""FastAPI application + HTTP surface for STARK."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from . import llm
from .agent import StarkAgent
from .approvals import DECISIONS, OUTCOMES, DecisionLog
from .benchmark import run_benchmark
from .graph import build_graph, build_timeline, families, graph_for_alert, incident_dossier
from .importer import SAMPLE_POSTMORTEM, parse_postmortem, to_incident
from .ingest import SAMPLE_PAYLOADS, Inbox, normalise
from .config import settings
from .ledger import IncidentLedger
from .memory import MemoryStore
from .seed_data import DEMO_ALERTS, INCIDENTS

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(
    title="STARK",
    description="An on-call incident response agent that learns from every incident, built on Hindsight.",
    version="1.0.0",
)

settings.state_dir.mkdir(parents=True, exist_ok=True)
store = MemoryStore(settings)
ledger = IncidentLedger(Path(settings.state_dir) / "ledger.json")
decisions = DecisionLog(Path(settings.state_dir) / "decisions.json")
inbox = Inbox(Path(settings.state_dir) / "inbox.json")
agent = StarkAgent(store, ledger, decisions)


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #


class TriageRequest(BaseModel):
    alert: str = Field(..., min_length=5, description="Raw alert text, log lines and all.")
    use_memory: bool = True


class CompareRequest(BaseModel):
    alert: str = Field(..., min_length=5)


class RecallRequest(BaseModel):
    query: str = Field(..., min_length=2)
    limit: int = 15


class DecisionRequest(BaseModel):
    recommendation_key: str
    decision: str = Field(..., description="approve | reject | investigate")
    action: str
    service: str
    failure_class: str = ""
    decided_by: str = "on-call"
    note: str = ""
    alert_title: str = ""

    @field_validator("decision")
    @classmethod
    def _known_decision(cls, v: str) -> str:
        if v not in DECISIONS:
            raise ValueError(f"decision must be one of {DECISIONS}")
        return v


class DecisionOutcomeRequest(BaseModel):
    recommendation_key: str
    outcome: str = Field(..., description="worked | did_not_work | unknown")
    note: str = ""

    @field_validator("outcome")
    @classmethod
    def _known_outcome(cls, v: str) -> str:
        if v not in OUTCOMES:
            raise ValueError(f"outcome must be one of {OUTCOMES}")
        return v


class ImportPreviewRequest(BaseModel):
    text: str = Field(..., min_length=40, description="Raw postmortem text")
    use_llm: bool = True


class ImportConfirmRequest(BaseModel):
    """The reviewed record. Nothing enters memory without passing through here."""

    incident_id: str
    service: str
    failure_class: str
    alert_title: str
    root_cause: str
    fix: str
    false_leads: str = ""
    symptoms: str = ""
    verification: str = ""
    customer_impact: str = ""
    severity: str = "SEV3"
    mttr_minutes: int = 0
    responder: str = "imported"


class OutcomeRequest(BaseModel):
    incident_id: str
    service: str
    failure_class: str
    alert_title: str
    root_cause: str
    fix: str
    mttr_minutes: int
    responder: str = "on-call"
    false_leads: str = ""
    severity: str = "SEV2"
    alert_payload: str = ""


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #


@app.get("/api/status")
def status() -> dict[str, Any]:
    stats = store.stats()
    return {
        "memory": stats,
        "memory_backend": store.mode,
        "hindsight_configured": settings.hindsight_enabled,
        "hindsight_base_url": settings.hindsight_base_url or None,
        "llm_configured": llm.available(),
        "llm_model": settings.groq_model if llm.available() else "deterministic-fallback",
        "seeded": stats.get("facts", 0) > 0,
        "seed_corpus_size": len(INCIDENTS),
        "ledger_entries": len(ledger.entries),
        "decisions": decisions.stats(),
        "inbox": inbox.stats(),
    }


@app.post("/api/seed")
def seed() -> dict[str, Any]:
    return agent.seed()


@app.post("/api/reset")
def reset() -> dict[str, Any]:
    store.reset()
    ledger.reset()
    decisions.reset()
    inbox.reset()
    return {"ok": True, "memory": store.stats()}


@app.get("/api/demo-alerts")
def demo_alerts() -> dict[str, Any]:
    return {"alerts": DEMO_ALERTS}


@app.post("/api/triage")
def triage(request: TriageRequest) -> dict[str, Any]:
    if store.stats().get("facts", 0) == 0 and request.use_memory:
        raise HTTPException(status_code=409, detail="Memory is empty. POST /api/seed first.")
    return agent.triage(request.alert, use_memory=request.use_memory)


@app.post("/api/compare")
def compare(request: CompareRequest) -> dict[str, Any]:
    if store.stats().get("facts", 0) == 0:
        raise HTTPException(status_code=409, detail="Memory is empty. POST /api/seed first.")
    return agent.compare(request.alert)


@app.post("/api/recall")
def recall(request: RecallRequest) -> dict[str, Any]:
    hits = store.recall(request.query, limit=request.limit)
    return {"query": request.query, "count": len(hits), "hits": [h.to_dict() for h in hits]}


@app.post("/api/outcome")
def outcome(request: OutcomeRequest) -> dict[str, Any]:
    return agent.record_outcome(**request.model_dump())


@app.post("/api/decision")
def decision(request: DecisionRequest) -> dict[str, Any]:
    """A human ruled on a recommendation. Recorded to the log and to memory."""
    return agent.record_decision(**request.model_dump())


@app.post("/api/decision/outcome")
def decision_outcome(request: DecisionOutcomeRequest) -> dict[str, Any]:
    """Did the approved action actually work?"""
    return agent.record_decision_outcome(**request.model_dump())


@app.get("/api/decisions")
def decision_log() -> dict[str, Any]:
    return {"stats": decisions.stats(), "recent": decisions.recent()}


@app.post("/api/webhook/alert")
def webhook_alert(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """
    Receive an alert from a monitoring system and triage it automatically.

    Accepts Prometheus Alertmanager, Datadog, Grafana and generic JSON shapes.
    An unrecognised body is flattened rather than rejected — a webhook endpoint
    that returns 400 at 3am is worse than one that does something imperfect.
    """
    alert = normalise(payload)
    if not alert.alert_text.strip():
        raise HTTPException(status_code=422, detail="Could not extract any alert text.")

    brief = agent.triage(alert.alert_text, use_memory=True)
    summary = {
        "verdict": brief["verdict"],
        "memory_grounded": brief["memory_grounded"],
        "confidence": brief["confidence"],
        "estimated_mttr_minutes": brief.get("estimated_mttr_minutes"),
        "cited": [c["incident_id"] for c in brief.get("recalled_incidents", [])][:4],
        "recommendations": len(brief.get("recommendations", [])),
        "spoken": brief.get("spoken", ""),
    }
    inbox.add(alert, summary)
    return {"received": alert.to_dict(), "triage": brief, "summary": summary}


@app.post("/api/webhook/simulate")
def webhook_simulate(source: str = Body("alertmanager", embed=True)) -> dict[str, Any]:
    """Fire one of the bundled vendor payloads at the webhook, for demos."""
    payload = SAMPLE_PAYLOADS.get(source)
    if payload is None:
        raise HTTPException(
            status_code=404, detail=f"Unknown sample source. Try one of {sorted(SAMPLE_PAYLOADS)}."
        )
    return webhook_alert(payload)


@app.get("/api/webhook/samples")
def webhook_samples() -> dict[str, Any]:
    return {"samples": {k: v for k, v in SAMPLE_PAYLOADS.items()}}


@app.get("/api/inbox")
def get_inbox() -> dict[str, Any]:
    return {"stats": inbox.stats(), "entries": inbox.recent()}


@app.get("/api/import/sample")
def import_sample() -> dict[str, Any]:
    return {"text": SAMPLE_POSTMORTEM}


@app.post("/api/import/preview")
def import_preview(request: ImportPreviewRequest) -> dict[str, Any]:
    """
    Extract a structured incident from a postmortem, for review.

    Deliberately does not retain anything. A wrong root cause in the bank is worse
    than no root cause, because it will be cited with confidence months later.
    """
    parsed = parse_postmortem(request.text, use_llm=request.use_llm)
    return {"parsed": parsed.to_dict(), "preview_incident": to_incident(parsed)}


@app.post("/api/import/confirm")
def import_confirm(request: ImportConfirmRequest) -> dict[str, Any]:
    """Retain a reviewed postmortem as memory."""
    result = agent.record_outcome(
        incident_id=request.incident_id,
        service=request.service,
        failure_class=request.failure_class,
        alert_title=request.alert_title,
        root_cause=request.root_cause,
        fix=request.fix,
        mttr_minutes=request.mttr_minutes,
        responder=request.responder,
        false_leads=request.false_leads,
        severity=request.severity,
        alert_payload=request.symptoms or request.alert_title,
    )
    return {**result, "imported": True}


@app.get("/api/memory/observations")
def observations() -> dict[str, Any]:
    return {"observations": store.observations()}


@app.get("/api/memory/graph")
def memory_graph(
    service: str = "", failure_class: str = "", incident_id: str = ""
) -> dict[str, Any]:
    """The structural view: what memory holds about a service or failure family."""
    return build_graph(
        service=service,
        failure_class=failure_class,
        incident_id=incident_id,
        decisions=decisions,
    )


@app.post("/api/memory/graph/for-alert")
def memory_graph_for_alert(request: CompareRequest) -> dict[str, Any]:
    """The situational view: only the memories this specific alert lit up."""
    fp, hits = agent.recall_for_alert(request.alert)
    assembled = agent.assemble(hits)
    agent.enrich(assembled, top_n=3)
    if not agent.score_relevance(fp, assembled):
        return {"nodes": [], "edges": [], "empty": True, "reason": "no memory cleared the gate"}
    return graph_for_alert(assembled, decisions=decisions)


@app.get("/api/memory/timeline")
def memory_timeline(failure_class: str = "") -> dict[str, Any]:
    """One failure family in order, with what memory gained at each occurrence."""
    if not failure_class:
        return {"families": families()}
    return {"families": families(), **build_timeline(failure_class)}


@app.get("/api/why/{incident_id}")
def why(incident_id: str) -> dict[str, Any]:
    """Every retained fact behind one incident — the evidence for a recommendation."""
    dossier = incident_dossier(store, incident_id.upper())
    if dossier["empty"]:
        raise HTTPException(status_code=404, detail=f"Memory holds nothing for {incident_id}.")
    return dossier


_benchmark_cache: dict[str, Any] = {}


@app.get("/api/benchmark")
def benchmark(refresh: bool = False) -> dict[str, Any]:
    """
    Controlled A/B: every incident scored twice, memory off and memory on.

    Leave-one-out — the incident under test is hidden from memory, so the memory
    arm cannot simply read the answer. Cached after the first run because the
    result is deterministic for a given corpus.
    """
    if refresh or "result" not in _benchmark_cache:
        _benchmark_cache["result"] = run_benchmark(
            lambda store_, ledger_: StarkAgent(store_, ledger_), settings
        )
    return _benchmark_cache["result"]


@app.get("/api/learning-curve")
def learning_curve() -> dict[str, Any]:
    return agent.learning_curve()


@app.get("/api/incidents")
def incidents() -> dict[str, Any]:
    return {
        "count": len(INCIDENTS),
        "incidents": [
            {
                "incident_id": i["incident_id"],
                "opened_at": i["opened_at"],
                "service": i["service"],
                "severity": i["severity"],
                "failure_class": i["failure_class"],
                "alert_title": i["alert_title"],
                "mttr_minutes": i["mttr_minutes"],
                "responder": i["responder"],
            }
            for i in sorted(INCIDENTS, key=lambda x: x["opened_at"], reverse=True)
        ],
    }


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


# --------------------------------------------------------------------------- #
# Static UI
# --------------------------------------------------------------------------- #

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

else:  # pragma: no cover

    @app.get("/")
    def index_missing() -> JSONResponse:
        return JSONResponse({"detail": "static/index.html not found"}, status_code=404)


def main() -> None:
    import uvicorn

    uvicorn.run("app.main:app", host=settings.host, port=settings.port, reload=False)


if __name__ == "__main__":
    main()
