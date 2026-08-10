"""FastAPI application + HTTP surface for STARK."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from . import llm
from .agent import StarkAgent
from .approvals import DECISIONS, OUTCOMES, DecisionLog
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
    }


@app.post("/api/seed")
def seed() -> dict[str, Any]:
    return agent.seed()


@app.post("/api/reset")
def reset() -> dict[str, Any]:
    store.reset()
    ledger.reset()
    decisions.reset()
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


@app.get("/api/memory/observations")
def observations() -> dict[str, Any]:
    return {"observations": store.observations()}


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
