"""Tests for the spoken briefing."""

from __future__ import annotations

import pytest

from app.voice import _first_clause, _say_ids, speakable


def test_incident_ids_are_spoken_as_words():
    assert _say_ids("see INC-1131 and INC-1042") == "see incident 1131 and incident 1042"


def test_first_clause_stops_before_the_trailing_advice():
    text = "Kill the vacuum; route analytics to the reporting reader, and never run VACUUM FULL."
    out = _first_clause(text)
    assert out == "Kill the vacuum; route analytics to the reporting reader"
    assert "VACUUM FULL" not in out


def test_first_clause_skips_clauses_too_terse_to_be_useful():
    """A three-word head carries no instruction, so the next boundary is used."""
    assert _first_clause("Kill it; then drain the queue and restart the consumer group") != "Kill it"


def test_first_clause_never_exceeds_the_limit():
    assert len(_first_clause("word " * 200, limit=90)) <= 90


def test_first_clause_lands_on_a_word_boundary():
    out = _first_clause("supercalifragilistic " * 20, limit=50)
    assert not out.endswith("supercalifragilisti")


def test_grounded_brief_names_the_precedent_and_the_fix():
    spoken = speakable(
        {
            "memory_grounded": True,
            "similar_incidents": [{"incident_id": "INC-1131"}, {"incident_id": "INC-1042"}],
            "recalled_incidents": [{"incident_id": "INC-1131", "opened_at": "2025-12-01T20:38:00Z"}],
            "likely_root_causes": [{"cause": "PgBouncer default_pool_size was left at 20."}],
            "first_actions": [{"step": "Raise default_pool_size to 80.", "source_incident": "INC-1131"}],
            "do_not_do": [{"action": "Suspecting Aurora.", "source_incident": "INC-1131"}],
            "estimated_mttr_minutes": 52,
        }
    )
    assert "incident 1131" in spoken
    assert "INC-" not in spoken
    assert "December" in spoken
    assert "52 minutes" in spoken
    assert "not to repeat" in spoken


def test_ungrounded_brief_says_so_plainly():
    spoken = speakable(
        {
            "memory_grounded": False,
            "first_actions": [{"step": "Establish blast radius first."}],
            "do_not_do": [],
            "likely_root_causes": [],
        }
    )
    assert "no precedent" in spoken.lower()
    assert "record the outcome" in spoken


def test_spoken_briefing_stays_listenable():
    """Anything past ~110 words stops being a briefing and becomes a monologue."""
    from app.agent import StarkAgent  # noqa: PLC0415

    brief = {
        "memory_grounded": True,
        "similar_incidents": [{"incident_id": "INC-1"}],
        "recalled_incidents": [{"incident_id": "INC-1", "opened_at": "2025-12-01T00:00:00Z"}],
        "likely_root_causes": [{"cause": "cause " * 60}],
        "first_actions": [{"step": "step " * 60, "source_incident": "INC-1"}],
        "do_not_do": [{"action": "action " * 60, "source_incident": "INC-1"}],
        "estimated_mttr_minutes": 40,
    }
    assert len(speakable(brief).split()) < 110


@pytest.mark.parametrize("grounded", [True, False])
def test_triage_always_returns_a_spoken_field(grounded, tmp_path):
    from app.config import settings
    from app.ledger import IncidentLedger
    from app.memory import LocalMemoryEngine, MemoryStore
    from app.agent import StarkAgent
    from app.seed_data import DEMO_ALERTS

    store = MemoryStore.__new__(MemoryStore)
    store.backend = LocalMemoryEngine(None)
    store.mode = "local-test"
    store.settings = settings
    agent = StarkAgent(store, IncidentLedger(tmp_path / "l.json"))
    agent.seed()

    alert = DEMO_ALERTS[0]["text"] if grounded else DEMO_ALERTS[3]["text"]
    result = agent.triage(alert)
    assert result["spoken"]
    assert result["memory_grounded"] is grounded


def test_typographic_dashes_do_not_break_incident_ids():
    """An LLM writes INC‑1131 with a non-breaking hyphen; it must still be spoken."""
    assert _say_ids("see INC‑1131") == "see incident 1131"
