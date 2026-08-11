"""Tests for the human approval loop."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.approvals import (
    APPROVE,
    DID_NOT_WORK,
    INVESTIGATE,
    REJECT,
    WORKED,
    Decision,
    DecisionLog,
    classify_risk,
    decision_memory,
    rank_score,
    recommendation_key,
)
from app.config import settings
from app.ledger import IncidentLedger
from app.memory import LocalMemoryEngine, MemoryStore
from app.agent import StarkAgent
from app.seed_data import DEMO_ALERTS


@pytest.fixture()
def agent(tmp_path: Path) -> StarkAgent:
    store = MemoryStore.__new__(MemoryStore)
    store.backend = LocalMemoryEngine(None)
    store.mode = "local-test"
    store.settings = settings
    built = StarkAgent(
        store,
        IncidentLedger(tmp_path / "ledger.json"),
        DecisionLog(tmp_path / "decisions.json"),
    )
    built.seed()
    return built


# -- keys and risk ---------------------------------------------------------- #


def test_recommendation_keys_are_stable_across_runs():
    a = recommendation_key("payments-api", "pool", "Raise default_pool_size to 80.")
    b = recommendation_key("payments-api", "pool", "Raise default_pool_size to 80.")
    assert a == b


def test_keys_ignore_tuned_numbers_so_track_records_accumulate():
    """The same advice at a different setting is the same advice."""
    a = recommendation_key("payments-api", "pool", "Raise default_pool_size to 20.")
    b = recommendation_key("payments-api", "pool", "Raise default_pool_size to 80.")
    assert a == b


def test_keys_differ_across_services_and_actions():
    assert recommendation_key("a", "pool", "Restart it.") != recommendation_key("b", "pool", "Restart it.")
    assert recommendation_key("a", "pool", "Restart it.") != recommendation_key("a", "pool", "Check the logs.")


@pytest.mark.parametrize(
    "action,expected",
    [
        ("Roll back the retry endpoint", "high"),
        ("Restart the consumer group", "high"),
        ("Check hikaricp_connections_pending", "low"),
        ("Inspect the dashboards", "low"),
        ("Pin the previous ca-certificates package", "medium"),
    ],
)
def test_risk_is_a_property_of_the_verb_not_the_confidence(action, expected):
    assert classify_risk(action) == expected


# -- the log ---------------------------------------------------------------- #


def test_track_record_is_empty_before_any_decision(tmp_path):
    log = DecisionLog(tmp_path / "d.json")
    assert log.track_record("REC-nope") == {"seen": 0}


def test_track_record_counts_decisions_and_outcomes(tmp_path):
    log = DecisionLog(tmp_path / "d.json")
    for d in (APPROVE, REJECT, INVESTIGATE):
        log.add(Decision("REC-1", d, "do the thing", "svc", "fam", "priya"))
    log.record_outcome("REC-1", WORKED)
    t = log.track_record("REC-1")
    assert (t["approved"], t["rejected"], t["investigated"], t["worked"]) == (1, 1, 1, 1)
    assert "approved 1x" in t["summary"]


def test_rejection_reason_is_kept_for_next_time(tmp_path):
    log = DecisionLog(tmp_path / "d.json")
    log.add(Decision("REC-1", REJECT, "act", "svc", "fam", "priya", note="already at 80 in prod"))
    assert log.track_record("REC-1")["last_rejection_note"] == "already at 80 in prod"


def test_outcome_attaches_to_the_approval_not_the_rejection(tmp_path):
    log = DecisionLog(tmp_path / "d.json")
    log.add(Decision("REC-1", REJECT, "act", "svc", "fam", "sam"))
    log.add(Decision("REC-1", APPROVE, "act", "svc", "fam", "priya"))
    updated = log.record_outcome("REC-1", DID_NOT_WORK, "made it worse")
    assert updated.decision == APPROVE
    assert log.track_record("REC-1")["did_not_work"] == 1


def test_outcome_on_an_unapproved_recommendation_is_a_no_op(tmp_path):
    log = DecisionLog(tmp_path / "d.json")
    log.add(Decision("REC-1", REJECT, "act", "svc", "fam", "sam"))
    assert log.record_outcome("REC-1", WORKED) is None


def test_confirmed_success_outranks_a_bare_approval():
    assert rank_score({"seen": 1, "worked": 1, "approved": 1}) > rank_score({"seen": 1, "approved": 1})


def test_confirmed_failure_hurts_more_than_a_rejection():
    assert rank_score({"seen": 1, "did_not_work": 1}) < rank_score({"seen": 1, "rejected": 1})


def test_decision_memory_reads_like_something_worth_remembering():
    text, meta = decision_memory(
        Decision("REC-1", REJECT, "Raise the pool", "payments-api", "pool", "priya",
                 note="already at 80"),
        alert_title="p99 3,640ms",
    )
    assert "rejected STARK's recommendation" in text
    assert "already at 80" in text
    assert meta["kind"] == "decision"
    assert meta["decision"] == REJECT


# -- end to end ------------------------------------------------------------- #


def test_grounded_brief_produces_recommendations(agent: StarkAgent):
    brief = agent.triage(DEMO_ALERTS[0]["text"])
    assert brief["recommendations"]
    assert all(r["key"].startswith("REC-") for r in brief["recommendations"])
    assert all(r["expected_impact"] for r in brief["recommendations"])


def test_novel_alert_asks_nobody_to_approve_anything(agent: StarkAgent):
    """With no precedent, STARK has no business proposing a production change."""
    novel = next(a for a in DEMO_ALERTS if a["label"].startswith("Novel"))
    assert agent.triage(novel["text"])["recommendations"] == []


def test_a_decision_is_written_to_memory_as_an_experience_fact(agent: StarkAgent):
    rec = agent.triage(DEMO_ALERTS[0]["text"])["recommendations"][0]
    before = agent.store.stats()["experience_facts"]
    agent.record_decision(
        recommendation_key=rec["key"], decision=REJECT, action=rec["action"],
        service="payments-api", failure_class=rec["failure_class"],
        decided_by="priya", note="already at 80 in prod",
    )
    assert agent.store.stats()["experience_facts"] > before


def test_past_decisions_are_recalled_on_a_later_alert(agent: StarkAgent):
    rec = agent.triage(DEMO_ALERTS[0]["text"])["recommendations"][0]
    agent.record_decision(
        recommendation_key=rec["key"], decision=REJECT, action=rec["action"],
        service="payments-api", failure_class=rec["failure_class"],
        decided_by="priya", note="already at 80 in prod",
    )
    again = agent.triage(DEMO_ALERTS[0]["text"])
    assert again["prior_decisions"]
    assert any(p["decision"] == REJECT for p in again["prior_decisions"])


def test_a_confirmed_success_is_promoted_to_the_top(agent: StarkAgent):
    recs = agent.triage(DEMO_ALERTS[0]["text"])["recommendations"]
    # Pick one anchored to a source incident: its key survives the model
    # rewording the advice between runs, which is the whole point of the anchor.
    chosen = next(r for r in recs if r["source_incident"])
    agent.record_decision(
        recommendation_key=chosen["key"], decision=APPROVE, action=chosen["action"],
        service="payments-api", failure_class=chosen["failure_class"], decided_by="priya",
    )
    agent.record_decision_outcome(recommendation_key=chosen["key"], outcome=WORKED)
    after = agent.triage(DEMO_ALERTS[0]["text"])["recommendations"]
    assert after[0]["key"] == chosen["key"]
    assert after[0]["track_record"]["worked"] == 1


def test_a_rejected_action_is_demoted_but_still_visible(agent: StarkAgent):
    """Hiding it would lose the reason it was rejected — which is the useful part."""
    recs = agent.triage(DEMO_ALERTS[0]["text"])["recommendations"]
    rejected = next(r for r in recs if r["source_incident"])
    agent.record_decision(
        recommendation_key=rejected["key"], decision=REJECT, action=rejected["action"],
        service="payments-api", failure_class=rejected["failure_class"],
        decided_by="priya", note="already at 80 in prod",
    )
    after = agent.triage(DEMO_ALERTS[0]["text"])["recommendations"]
    match = next((r for r in after if r["key"] == rejected["key"]), None)
    assert match is not None, "a rejected recommendation must not vanish silently"
    assert after[0]["key"] != rejected["key"]
    assert match["track_record"]["last_rejection_note"] == "already at 80 in prod"


def test_reseeding_clears_the_decision_log(agent: StarkAgent):
    rec = agent.triage(DEMO_ALERTS[0]["text"])["recommendations"][0]
    agent.record_decision(
        recommendation_key=rec["key"], decision=APPROVE, action=rec["action"],
        service="payments-api", failure_class=rec["failure_class"], decided_by="priya",
    )
    assert agent.decisions.stats()["decisions"] == 1
    agent.seed()
    assert agent.decisions.stats()["decisions"] == 0


def test_key_survives_the_model_rewording_the_advice():
    """
    An LLM paraphrases every sentence it writes. If the key were derived from the
    text, a fresh key would be minted on every run and no team decision would ever
    stick to a recommendation — the approval loop would silently do nothing.
    """
    a = recommendation_key("payments-api", "pool", "Raise default_pool_size to 80", "INC-1131")
    b = recommendation_key("payments-api", "pool", "Increase the PgBouncer pool size", "INC-1131")
    assert a == b


def test_advice_from_different_incidents_stays_distinct():
    a = recommendation_key("payments-api", "pool", "same words", "INC-1131")
    b = recommendation_key("payments-api", "pool", "same words", "INC-1042")
    assert a != b


def test_unsourced_advice_still_keys_on_its_text():
    """Curated playbook steps have no source incident to anchor to."""
    a = recommendation_key("payments-api", "pool", "Check hikaricp_connections_pending")
    b = recommendation_key("payments-api", "pool", "Check hikaricp_connections_pending")
    assert a == b
