"""Tests for the controlled A/B benchmark."""

from __future__ import annotations

import pytest

from app.agent import StarkAgent
from app.benchmark import ROW_META, _echoes, _stem, _terms, run_benchmark
from app.config import settings
from app.seed_data import INCIDENTS


@pytest.fixture(scope="module")
def result() -> dict:
    return run_benchmark(lambda store, ledger: StarkAgent(store, ledger), settings)


@pytest.mark.parametrize(
    "a,b",
    [
        ("scaled", "scale"),
        ("restarted", "restart"),
        ("deploys", "deploy"),
        ("rolling", "roll"),
        ("caches", "cache"),
        ("services", "service"),
    ],
)
def test_stemming_is_symmetric_across_word_forms(a, b):
    """An asymmetric comparison silently never matches; assert the pair, not the form."""
    assert _stem(a) == _stem(b)


def test_short_words_are_left_alone():
    assert _stem("pods") == "pods"


def test_echoes_needs_two_shared_terms():
    """One coincidental word must not count as repeating a mistake."""
    assert not _echoes("Scale the pods", "The deploy was innocent")
    assert _echoes("Roll back the recent deploy", "Team almost rolled back a deploy, it was innocent")


def test_echoes_returns_its_evidence():
    shared = _echoes("Roll back the recent deploy", "Team almost rolled back a deploy from that afternoon")
    assert "deploy" in shared and len(shared) >= 2


def test_both_arms_score_every_incident(result):
    off, on = result["arms"]
    assert off["scored"] == on["scored"] == len(INCIDENTS)


def test_leave_one_out_hides_the_incident_under_test(result):
    """If the incident under test were visible, it would cite itself."""
    for row in result["rows"]["with_memory"]:
        assert row["incident_id"] not in row["cited"]


def test_memory_arm_cites_evidence_and_the_other_cannot(result):
    off, on = result["arms"]
    assert off["evidence_citations"] == 0
    assert on["evidence_citations"] > 20


def test_mechanism_row_is_reported_but_never_claimed_as_a_win(result):
    """
    Memory does not make the model a better diagnostician, and asserting that it
    does encodes a claim that is false on the LLM path — a capable model reads the
    failure mechanism straight off the alert signature. The row must exist, be
    flagged path-dependent, and say so in its own note.
    """
    off, on = result["arms"]
    assert "identified_failure_family" in off and "identified_failure_family" in on
    meta = ROW_META["identified_failure_family"]
    assert meta["path_dependent"] is True
    assert "not a memory win" in meta["note"].lower()


def test_memory_arm_gives_more_concrete_advice(result):
    off, on = result["arms"]
    assert on["concrete_action"] > off["concrete_action"]


def test_memory_arm_repeats_fewer_known_dead_ends(result):
    off, on = result["arms"]
    assert on["repeats_known_dead_end"] <= off["repeats_known_dead_end"]


def test_every_dead_end_hit_carries_an_audit_trail(result):
    for arm in result["rows"].values():
        for row in arm:
            if row["repeats_known_dead_end"]:
                match = row["dead_end_match"]
                assert match and match["shared_terms"] and match["dead_end"]


def test_constructive_rows_are_labelled_as_such():
    """Rows the no-memory arm cannot win must not be presented as findings."""
    assert ROW_META["cited_correct_precedent"]["constructive"] is True
    assert ROW_META["false_precedent"]["constructive"] is True
    assert ROW_META["identified_failure_family"]["constructive"] is False
    assert ROW_META["repeats_known_dead_end"]["constructive"] is False


def test_result_declares_its_method_and_its_limits(result):
    assert "leave-one-out" in result["method"].lower()
    assert "synthetic" in result["caveat"].lower()
    assert result["headline"]["statement"]
