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


def test_webhook_accepts_every_vendor_shape(client: TestClient):
    from app.ingest import SAMPLE_PAYLOADS

    for name, payload in SAMPLE_PAYLOADS.items():
        r = client.post("/api/webhook/alert", json=payload)
        assert r.status_code == 200, name
        body = r.json()
        assert body["received"]["alert_text"]
        assert "verdict" in body["summary"]


def test_webhook_triages_a_known_family_on_arrival(client: TestClient):
    body = client.post("/api/webhook/simulate", json={"source": "alertmanager"}).json()
    assert body["summary"]["memory_grounded"] is True
    assert body["summary"]["cited"]


def test_webhook_stays_honest_about_a_novel_alert(client: TestClient):
    body = client.post("/api/webhook/simulate", json={"source": "generic"}).json()
    assert body["summary"]["memory_grounded"] is False
    assert body["summary"]["recommendations"] == 0


def test_webhook_rejects_a_payload_with_no_extractable_text(client: TestClient):
    assert client.post("/api/webhook/alert", json={}).status_code == 422


def test_unknown_simulate_source_is_a_404(client: TestClient):
    assert client.post("/api/webhook/simulate", json={"source": "nope"}).status_code == 404


def test_inbox_records_what_arrived(client: TestClient):
    client.post("/api/webhook/simulate", json={"source": "datadog"})
    body = client.get("/api/inbox").json()
    assert body["stats"]["received"] >= 1
    assert body["entries"][0]["alert"]["source"]


def test_memory_graph_endpoint(client: TestClient):
    g = client.get("/api/memory/graph", params={"service": "payments-api"}).json()
    assert g["nodes"] and g["edges"]
    assert all("x" in n and "y" in n for n in g["nodes"])


def test_graph_for_alert_is_empty_when_the_gate_refuses(client: TestClient):
    novel = next(a for a in client.get("/api/demo-alerts").json()["alerts"]
                 if a["label"].startswith("Novel"))
    g = client.post("/api/memory/graph/for-alert", json={"alert": novel["text"]}).json()
    assert g["empty"] is True


def test_timeline_endpoint(client: TestClient):
    t = client.get("/api/memory/timeline", params={"failure_class": "kafka-consumer-lag"}).json()
    assert t["occurrences"] == 3
    assert t["entries"][0]["memory_before"] == 0


def test_why_endpoint_returns_the_evidence(client: TestClient):
    d = client.get("/api/why/INC-1131").json()
    assert "fix" in d["facts"]
    assert client.get("/api/why/INC-0000").status_code == 404


def test_import_preview_does_not_retain_anything(client: TestClient):
    """Review before retention: a wrong root cause in memory is worse than none."""
    text = client.get("/api/import/sample").json()["text"]
    before = client.get("/api/status").json()["memory"]["facts"]
    body = client.post("/api/import/preview", json={"text": text, "use_llm": False}).json()
    assert body["parsed"]["incident_id"] == "INC-2291"
    assert client.get("/api/status").json()["memory"]["facts"] == before


def test_importing_a_postmortem_makes_a_novel_alert_grounded(client: TestClient):
    alert = ("ALERT: CheckoutApi502\nservice=checkout-api\n"
             "log: upstream connect error or disconnect/reset before headers\n502 rate 30%")
    assert client.post("/api/triage", json={"alert": alert}).json()["memory_grounded"] is False

    parsed = client.post("/api/import/preview",
                         json={"text": client.get("/api/import/sample").json()["text"],
                               "use_llm": False}).json()["parsed"]
    fields = ("incident_id", "service", "failure_class", "alert_title", "root_cause", "fix",
              "false_leads", "symptoms", "verification", "customer_impact", "severity",
              "mttr_minutes", "responder")
    client.post("/api/import/confirm", json={k: parsed[k] for k in fields})

    after = client.post("/api/triage", json={"alert": alert}).json()
    assert after["memory_grounded"] is True
    assert "INC-2291" in [c["incident_id"] for c in after["recalled_incidents"]]
    assert after["do_not_do"], "the imported dead end should be recalled"


def test_import_rejects_a_document_too_short_to_parse(client: TestClient):
    assert client.post("/api/import/preview", json={"text": "broke"}).status_code == 422


def test_benchmark_endpoint(client: TestClient):
    r = client.get("/api/benchmark").json()
    assert [a["arm"] for a in r["arms"]] == ["no memory", "STARK memory"]
    assert r["headline"]["statement"]
    assert "leave-one-out" in r["method"].lower()
