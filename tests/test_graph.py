"""Tests for the memory graph, timeline, and the why-does-STARK-believe-this view."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.approvals import APPROVE, Decision, DecisionLog
from app.config import settings
from app.graph import build_graph, build_timeline, families, incident_dossier
from app.ledger import IncidentLedger
from app.memory import LocalMemoryEngine, MemoryStore
from app.agent import StarkAgent


@pytest.fixture()
def agent(tmp_path: Path) -> StarkAgent:
    store = MemoryStore.__new__(MemoryStore)
    store.backend = LocalMemoryEngine(None)
    store.mode = "local-test"
    store.settings = settings
    built = StarkAgent(store, IncidentLedger(tmp_path / "l.json"), DecisionLog(tmp_path / "d.json"))
    built.seed()
    return built


def test_service_graph_connects_service_to_family_to_incident():
    g = build_graph(service="payments-api")
    kinds = {n["kind"] for n in g["nodes"]}
    assert {"service", "family", "incident", "cause", "fix"} <= kinds
    assert any(e["kind"] == "has hit" for e in g["edges"])
    assert any(e["kind"] == "resolved by" for e in g["edges"])


def test_dead_ends_appear_as_their_own_nodes():
    g = build_graph(failure_class="kafka-consumer-lag")
    assert any(n["kind"] == "false_lead" for n in g["nodes"])


def test_layout_is_deterministic():
    """A node that moves between renders cannot be pointed at on stage."""
    a = {n["id"]: (n["x"], n["y"]) for n in build_graph(service="payments-api")["nodes"]}
    b = {n["id"]: (n["x"], n["y"]) for n in build_graph(service="payments-api")["nodes"]}
    assert a == b


def test_columns_flow_left_to_right_from_cause_to_resolution():
    by_kind = {n["kind"]: n["column"] for n in build_graph(service="payments-api")["nodes"]}
    assert by_kind["service"] < by_kind["family"] < by_kind["incident"] < by_kind["fix"]


def test_unfiltered_graph_stays_a_skeleton():
    """Drawing every fact in the corpus at once is unreadable, so it is not drawn."""
    g = build_graph()
    assert g["skeleton"] is True
    assert not any(n["kind"] == "cause" for n in g["nodes"])


def test_unknown_filter_returns_empty_rather_than_raising():
    assert build_graph(service="does-not-exist")["empty"] is True


def test_decisions_hang_off_the_family_they_were_made_about(tmp_path):
    log = DecisionLog(tmp_path / "d.json")
    log.add(Decision("REC-1", APPROVE, "Raise the pool", "payments-api",
                     "db-connection-pool-exhaustion", "priya", note="worked last time"))
    g = build_graph(service="payments-api", decisions=log)
    decision_nodes = [n for n in g["nodes"] if n["kind"] == "decision"]
    assert decision_nodes
    assert "priya" in decision_nodes[0]["detail"]


# -- timeline --------------------------------------------------------------- #


def test_timeline_is_chronological_and_counts_prior_memory():
    t = build_timeline("db-connection-pool-exhaustion")
    assert [e["incident_id"] for e in t["entries"]] == ["INC-1042", "INC-1088", "INC-1131"]
    assert [e["memory_before"] for e in t["entries"]] == [0, 1, 2]
    assert t["entries"][0]["first_of_family"] is True


def test_timeline_of_an_unknown_family_is_empty():
    assert build_timeline("nope")["empty"] is True


def test_families_are_ranked_by_how_often_they_recur():
    counts = [f["occurrences"] for f in families()]
    assert counts == sorted(counts, reverse=True)


# -- dossier ---------------------------------------------------------------- #


def test_dossier_returns_every_kind_of_fact_for_an_incident(agent: StarkAgent):
    """A dossier that silently omits the fix is worse than no dossier at all."""
    d = incident_dossier(agent.store, "INC-1131")
    assert {"root_cause", "fix", "false_lead", "verification"} <= set(d["facts"])
    assert d["meta"]["service"] == "payments-api"


def test_dossier_never_leaks_another_incidents_facts(agent: StarkAgent):
    d = incident_dossier(agent.store, "INC-1131")
    for texts in d["facts"].values():
        for text in texts:
            assert "INC-1042" not in text or "INC-1131" in text


def test_dossier_of_an_unknown_incident_is_empty(agent: StarkAgent):
    assert incident_dossier(agent.store, "INC-0000")["empty"] is True
