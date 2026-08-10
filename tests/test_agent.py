"""Tests for the triage agent: fingerprinting, grounding, and the learning loop."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.agent import StarkAgent, build_recall_query, fingerprint
from app.config import settings
from app.ledger import IncidentLedger
from app.memory import LocalMemoryEngine, MemoryStore
from app.seed_data import DEMO_ALERTS, INCIDENTS


@pytest.fixture()
def agent(tmp_path: Path) -> StarkAgent:
    store = MemoryStore.__new__(MemoryStore)
    store.backend = LocalMemoryEngine(None)
    store.mode = "local-test"
    store.settings = settings
    ledger = IncidentLedger(tmp_path / "ledger.json")
    built = StarkAgent(store, ledger)
    built.seed()
    return built


# -- fingerprinting --------------------------------------------------------- #


def test_fingerprint_reads_explicit_service_label():
    assert fingerprint("service=payments-api\nsomething broke")["service"] == "payments-api"


def test_fingerprint_infers_service_from_camelcase_alert_name():
    assert fingerprint("ALERT: AuthGateway401Spike\n401 rate 24%")["service"] == "auth-gateway"


def test_fingerprint_collects_log_signatures():
    fp = fingerprint("ALERT: X\nlog: HikariPool-1 - Connection is not available")
    assert any("Hikari" in s for s in fp["signatures"])


def test_recall_query_includes_service_and_signatures():
    text = "ALERT: PaymentsApiLatencyHigh\nservice=payments-api\nlog: HikariPool-1 timeout"
    query = build_recall_query(text, fingerprint(text))
    assert "payments-api" in query and "Hikari" in query


# -- seeding ---------------------------------------------------------------- #


def test_seed_loads_every_incident(agent: StarkAgent):
    stats = agent.store.stats()
    assert stats["incidents"] == len(INCIDENTS)
    assert stats["observations"] >= 5
    assert stats["mental_models"] == 3


def test_seed_is_idempotent(agent: StarkAgent):
    before = agent.store.stats()["facts"]
    agent.seed()
    assert agent.store.stats()["facts"] == before


# -- triage ----------------------------------------------------------------- #


def test_repeat_alert_is_grounded_and_cites_the_right_family(agent: StarkAgent):
    result = agent.triage(DEMO_ALERTS[0]["text"])
    assert result["memory_grounded"] is True
    families = {c["failure_class"] for c in result["recalled_incidents"]}
    assert "db-connection-pool-exhaustion" in families
    assert result["estimated_mttr_minutes"]


def test_grounded_brief_warns_about_past_false_leads(agent: StarkAgent):
    result = agent.triage(DEMO_ALERTS[0]["text"])
    assert result["do_not_do"]
    assert all(w["source_incident"].startswith("INC-") for w in result["do_not_do"])


def test_novel_alert_is_not_force_fitted_onto_history(agent: StarkAgent):
    novel = next(a for a in DEMO_ALERTS if a["label"].startswith("Novel"))
    result = agent.triage(novel["text"])
    assert result["memory_grounded"] is False
    assert result["recalled_incidents"] == []


def test_memory_off_produces_no_citations(agent: StarkAgent):
    result = agent.triage(DEMO_ALERTS[0]["text"], use_memory=False)
    assert result["mode"] == "no-memory"
    assert result["similar_incidents"] == []
    assert result["memory_used"] == 0


def test_compare_shows_a_measurable_delta(agent: StarkAgent):
    result = agent.compare(DEMO_ALERTS[0]["text"])
    assert result["with_memory"]["memory_grounded"]
    assert not result["without_memory"]["memory_grounded"]
    assert result["delta"]["memories_recalled"] > 0
    assert result["delta"]["warnings_from_past_mistakes"] > 0


def test_every_demo_alert_behaves_as_advertised(agent: StarkAgent):
    for alert in DEMO_ALERTS:
        result = agent.triage(alert["text"])
        expected_grounded = not alert["label"].startswith("Novel")
        assert result["memory_grounded"] is expected_grounded, alert["label"]


# -- write-back ------------------------------------------------------------- #


def test_recording_an_outcome_makes_the_next_identical_alert_grounded(agent: StarkAgent):
    novel = next(a for a in DEMO_ALERTS if a["label"].startswith("Novel"))
    assert agent.triage(novel["text"])["memory_grounded"] is False

    agent.record_outcome(
        incident_id="INC-9001",
        service="webhook-dispatcher",
        failure_class="merchant-tls-trust-failure",
        alert_title="webhook delivery success rate 61%, x509 unknown authority",
        root_cause="A CA bundle update dropped two intermediate roots.",
        fix="Pin the previous ca-certificates package and redeploy.",
        false_leads="Twenty minutes lost on merchant firewall rules.",
        mttr_minutes=58,
    )

    after = agent.triage(novel["text"])
    assert after["memory_grounded"] is True
    assert "INC-9001" in [c["incident_id"] for c in after["recalled_incidents"]]
    assert after["estimated_mttr_minutes"] == 58


def test_recorded_outcome_lands_in_the_ledger(agent: StarkAgent):
    before = len(agent.ledger.entries)
    agent.record_outcome(
        incident_id="INC-9002",
        service="payments-api",
        failure_class="db-connection-pool-exhaustion",
        alert_title="pool saturated again",
        root_cause="pool too small",
        fix="resize",
        mttr_minutes=20,
    )
    assert len(agent.ledger.entries) == before + 1
    assert agent.ledger.entries[-1].had_prior_family_incident is True


# -- evaluation ------------------------------------------------------------- #


def test_learning_curve_is_a_held_out_replay(agent: StarkAgent):
    curve = agent.learning_curve()
    assert curve["total_incidents"] == len(INCIDENTS)
    # the first incident has nothing to recall, by construction
    assert curve["points"][0]["memory_size"] == 0
    assert curve["points"][0]["coverage"] == 0.0
    # coverage must be non-decreasing in the count of correct briefs
    assert curve["coverage"] > 0
    assert curve["recall_when_precedent_exists"] >= 0.8


def test_learning_curve_never_looks_ahead(agent: StarkAgent):
    points = agent.learning_curve()["points"]
    sizes = [p["memory_size"] for p in points]
    assert sizes == sorted(sizes)


def test_mttr_summary_separates_first_time_from_repeats(agent: StarkAgent):
    summary = agent.ledger.mttr_summary()
    assert summary["first_of_family_count"] + summary["repeat_count"] == len(INCIDENTS)
    assert summary["repeat_mean_mttr"] < summary["first_of_family_mean_mttr"]


def test_model_output_with_typographic_dashes_is_normalised():
    """
    LLMs emit U+2011 and friends where a human types a hyphen. They render
    identically in a browser and silently break every exact-match comparison —
    including INC-1131, which is how a citation is matched back to memory.
    """
    from app.agent import _normalise_brief, normalise_dashes

    assert normalise_dashes("ca‑certificates") == "ca-certificates"
    assert normalise_dashes(["INC‑1131"]) == ["INC-1131"]
    assert normalise_dashes({"a": {"b": "en–dash"}}) == {"a": {"b": "en-dash"}}

    brief = _normalise_brief({
        "verdict": "webhook‑dispatcher failed",
        "similar_incidents": [{"incident_id": "INC‑1161", "why": "same‑family"}],
    })
    assert brief["verdict"] == "webhook-dispatcher failed"
    assert brief["similar_incidents"][0]["incident_id"] == "INC-1161"
