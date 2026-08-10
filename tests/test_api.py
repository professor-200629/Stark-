"""End-to-end tests over the HTTP surface."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture(scope="module")
def client() -> TestClient:
    c = TestClient(app)
    c.post("/api/seed")
    return c


def test_healthz(client: TestClient):
    assert client.get("/healthz").json() == {"status": "ok"}


def test_status_reports_backend_and_llm_mode(client: TestClient):
    body = client.get("/api/status").json()
    assert body["memory_backend"] in {"local", "hindsight"}
    assert body["seeded"] is True
    assert body["memory"]["incidents"] > 0


def test_ui_is_served(client: TestClient):
    response = client.get("/")
    assert response.status_code == 200
    assert "STARK" in response.text


def test_demo_alerts_are_available(client: TestClient):
    alerts = client.get("/api/demo-alerts").json()["alerts"]
    assert len(alerts) >= 4
    assert all({"label", "text", "expect"} <= set(a) for a in alerts)


def test_triage_endpoint(client: TestClient):
    alert = client.get("/api/demo-alerts").json()["alerts"][0]["text"]
    body = client.post("/api/triage", json={"alert": alert}).json()
    assert body["memory_grounded"] is True
    assert body["memory_used"] > 0


def test_compare_endpoint_returns_both_sides(client: TestClient):
    alert = client.get("/api/demo-alerts").json()["alerts"][0]["text"]
    body = client.post("/api/compare", json={"alert": alert}).json()
    assert set(body) >= {"without_memory", "with_memory", "delta"}


def test_recall_endpoint_exposes_raw_memory(client: TestClient):
    body = client.post("/api/recall", json={"query": "payments-api connection pool"}).json()
    assert body["count"] > 0
    assert "strategies" in body["hits"][0]


def test_observations_endpoint(client: TestClient):
    observations = client.get("/api/memory/observations").json()["observations"]
    assert observations
    assert observations[0]["proof_count"] >= 2


def test_learning_curve_endpoint(client: TestClient):
    body = client.get("/api/learning-curve").json()
    assert body["total_incidents"] > 0
    assert "confusion" in body


def test_triage_rejects_empty_alert(client: TestClient):
    assert client.post("/api/triage", json={"alert": "x"}).status_code == 422


def test_decision_endpoints(client: TestClient):
    alert = client.get("/api/demo-alerts").json()["alerts"][0]["text"]
    rec = client.post("/api/triage", json={"alert": alert}).json()["recommendations"][0]

    res = client.post("/api/decision", json={
        "recommendation_key": rec["key"], "decision": "approve", "action": rec["action"],
        "service": "payments-api", "failure_class": rec["failure_class"],
        "decided_by": "test", "note": "looks right",
    }).json()
    assert res["recorded"]["decision"] == "approve"
    assert "approved STARK" in res["memory"]

    out = client.post("/api/decision/outcome", json={
        "recommendation_key": rec["key"], "outcome": "worked",
    }).json()
    assert out["updated"] is True
    assert out["track_record"]["worked"] == 1

    log = client.get("/api/decisions").json()
    assert log["stats"]["approved"] >= 1
    assert log["recent"]


def test_decision_rejects_an_unknown_verb(client: TestClient):
    body = {"recommendation_key": "REC-x", "decision": "maybe", "action": "a", "service": "s"}
    assert client.post("/api/decision", json=body).status_code == 422
