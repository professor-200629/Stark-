"""Tests for postmortem import — the path from real incidents into memory."""

from __future__ import annotations

import pytest

from app.importer import (
    SAMPLE_POSTMORTEM,
    parse_headings,
    parse_postmortem,
    to_incident,
)


def test_sample_postmortem_parses_without_a_model():
    p = parse_postmortem(SAMPLE_POSTMORTEM, use_llm=False)
    assert p.method == "headings"
    assert p.incident_id == "INC-2291"
    assert p.service == "checkout-api"
    assert p.severity == "SEV1"
    assert p.mttr_minutes == 47
    assert p.confidence == "high"
    assert not p.missing


def test_the_dead_end_survives_the_round_trip():
    """The false lead is the most valuable field; losing it defeats the point."""
    p = parse_postmortem(SAMPLE_POSTMORTEM, use_llm=False)
    assert "rolled back" in p.false_leads
    assert "waste of time" in p.false_leads


def test_markdown_and_inline_headings_both_work():
    doc = """# Incident
Root cause: the pool was exhausted
## Resolution
Raise the pool size to 80.
"""
    sections = parse_headings(doc)
    assert sections["root_cause"] == "the pool was exhausted"
    assert "Raise the pool size" in sections["fix"]


@pytest.mark.parametrize(
    "heading",
    ["Root cause", "What went wrong", "Contributing factors", "Diagnosis"],
)
def test_root_cause_synonyms_are_recognised(heading):
    assert "root_cause" in parse_headings(f"## {heading}\nthe disk filled up\n")


@pytest.mark.parametrize(
    "heading",
    ["Resolution", "Remediation", "What fixed it", "Corrective action"],
)
def test_fix_synonyms_are_recognised(heading):
    assert "fix" in parse_headings(f"## {heading}\nwe cleared the disk\n")


@pytest.mark.parametrize(
    "heading", ["Things we tried", "What didn't work", "Dead ends", "Red herrings"]
)
def test_dead_end_synonyms_are_recognised(heading):
    assert "false_leads" in parse_headings(f"## {heading}\nwe blamed the network\n")


def test_hours_are_converted_to_minutes():
    assert parse_postmortem("Incident\nresolved in 2 hours\n", use_llm=False).mttr_minutes == 120


def test_a_thin_document_is_flagged_rather_than_guessed():
    p = parse_postmortem("Something broke last Tuesday and then it was fine.", use_llm=False)
    assert p.confidence == "low"
    assert "root_cause" in p.missing
    assert any("Missing" in w for w in p.warnings)


def test_a_missing_dead_end_produces_a_warning():
    doc = "Incident\nservice: pay-api\nRoot cause: bad config\nResolution: fixed the config\n"
    p = parse_postmortem(doc, use_llm=False)
    assert any("dead end" in w.lower() for w in p.warnings)


def test_nothing_is_invented_for_an_empty_document():
    p = parse_postmortem("", use_llm=False)
    assert p.root_cause == ""
    assert p.fix == ""
    assert p.confidence == "low"


def test_failure_class_is_derived_so_imports_can_form_families():
    p = parse_postmortem(SAMPLE_POSTMORTEM, use_llm=False)
    assert p.failure_class and " " not in p.failure_class


def test_to_incident_produces_the_shape_the_rest_of_stark_expects():
    incident = to_incident(parse_postmortem(SAMPLE_POSTMORTEM, use_llm=False))
    required = {
        "incident_id", "opened_at", "service", "severity", "responder", "failure_class",
        "alert_title", "alert_payload", "symptoms", "false_leads", "root_cause", "fix",
        "verification", "mttr_minutes", "customer_impact", "tags",
    }
    assert required <= set(incident)
    assert "imported" in incident["tags"]
