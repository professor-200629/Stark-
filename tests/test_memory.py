"""Tests for the memory layer."""

from __future__ import annotations

import pytest

from app.memory import (
    EXPERIENCE,
    OBSERVATION,
    LocalMemoryEngine,
    MemoryItem,
    extract_entities,
    tokenize,
)


@pytest.fixture()
def engine() -> LocalMemoryEngine:
    return LocalMemoryEngine(None)


def test_tokenize_drops_stopwords():
    assert "the" not in tokenize("the payments-api is down")
    assert "payments-api" in tokenize("the payments-api is down")


def test_extract_entities_finds_services_and_signatures():
    entities = extract_entities("payments-api returned HTTP 503, pod was OOMKilled, redis evicted keys")
    assert "payments-api" in entities
    assert any("oomkilled" in e for e in entities)
    assert "redis" in entities


def test_extract_entities_handles_camelcase_alert_names():
    assert "auth-gateway" in extract_entities("ALERT: AuthGateway401Spike")


def test_retain_splits_multiline_content_into_atomic_facts(engine):
    engine.retain([MemoryItem(content="line one\nline two\nline three")])
    assert engine.stats()["facts"] == 3


def test_recall_ranks_relevant_facts_first(engine):
    engine.retain(
        [
            MemoryItem(content="payments-api suffered connection pool exhaustion"),
            MemoryItem(content="checkout-web had a bundle size regression"),
        ]
    )
    hits = engine.recall("payments-api pool exhausted")
    assert hits
    assert "payments-api" in hits[0].text


def test_recall_reports_which_strategies_fired(engine):
    engine.retain([MemoryItem(content="redis-sessions evicted 40000 keys per minute")])
    hits = engine.recall("redis eviction storm")
    assert hits and hits[0].strategies


def test_observations_require_at_least_two_incidents(engine):
    meta = {"service": "payments-api", "failure_class": "pool", "incident_id": "INC-1", "fix": "resize pool"}
    engine.retain([MemoryItem(content="first incident", metadata=meta)])
    assert engine.stats()["observations"] == 0

    meta2 = {**meta, "incident_id": "INC-2"}
    engine.retain([MemoryItem(content="second incident", metadata=meta2)])
    assert engine.stats()["observations"] == 1


def test_observation_carries_proof_count_and_recommended_fix(engine):
    for i in range(3):
        engine.retain(
            [
                MemoryItem(
                    content=f"payments-api pool exhausted, incident {i}",
                    metadata={
                        "service": "payments-api",
                        "failure_class": "pool",
                        "incident_id": f"INC-{i}",
                        "fix": "enable pgbouncer",
                        "mttr_minutes": 40 + i,
                    },
                )
            ]
        )
    observation = engine.observations()[0]
    assert observation["proof_count"] == 3
    assert observation["recommended_fix"] == "enable pgbouncer"
    assert observation["evidence"]


def test_experience_facts_outrank_world_facts_at_equal_relevance(engine):
    engine.retain([MemoryItem(content="kafka lag was high", type="world")])
    engine.retain([MemoryItem(content="kafka lag was high", type=EXPERIENCE)])
    hits = engine.recall("kafka lag")
    # dedupe keeps the highest-scoring copy; experience carries the higher priority
    assert hits[0].type == EXPERIENCE


def test_recall_can_filter_by_type(engine):
    engine.retain(
        [
            MemoryItem(content="payments-api pool exhausted", metadata={"service": "payments-api", "failure_class": "pool", "incident_id": "A", "fix": "x"}),
            MemoryItem(content="payments-api pool exhausted again", metadata={"service": "payments-api", "failure_class": "pool", "incident_id": "B", "fix": "x"}),
        ]
    )
    hits = engine.recall("payments-api pool", types=[OBSERVATION])
    assert hits and all(h.type == OBSERVATION for h in hits)


def test_empty_memory_returns_no_hits(engine):
    assert engine.recall("anything at all") == []
