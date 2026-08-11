"""Tests for webhook ingestion — the incident arriving rather than being pasted."""

from __future__ import annotations

import pytest

from app.ingest import (
    ALERTMANAGER,
    DATADOG,
    GENERIC,
    GRAFANA,
    UNKNOWN,
    SAMPLE_PAYLOADS,
    Inbox,
    IncomingAlert,
    detect_format,
    normalise,
)


@pytest.mark.parametrize(
    "name,expected",
    [
        ("alertmanager", ALERTMANAGER),
        ("datadog", DATADOG),
        ("grafana", GRAFANA),
        ("generic", GENERIC),
    ],
)
def test_each_vendor_shape_is_detected(name, expected):
    assert detect_format(SAMPLE_PAYLOADS[name]) == expected


@pytest.mark.parametrize("name", sorted(SAMPLE_PAYLOADS))
def test_every_sample_yields_a_service_and_a_log_signature(name):
    alert = normalise(SAMPLE_PAYLOADS[name])
    assert alert.service, f"{name} lost the service name"
    assert alert.alert_text.strip()
    assert "ALERT:" in alert.alert_text


def test_alertmanager_keeps_the_log_line_the_fingerprinter_needs():
    alert = normalise(SAMPLE_PAYLOADS["alertmanager"])
    assert "HikariPool-1" in alert.alert_text
    assert alert.service == "payments-api"
    assert alert.severity == "critical"


def test_datadog_service_is_read_out_of_the_tag_list():
    assert normalise(SAMPLE_PAYLOADS["datadog"]).service == "ledger-worker"


def test_grafana_eval_matches_become_metric_lines():
    assert "http_401_ratio=0.24" in normalise(SAMPLE_PAYLOADS["grafana"]).alert_text


def test_an_unrecognised_payload_is_flattened_not_rejected():
    """A webhook that 400s at 3am is worse than one that does something imperfect."""
    alert = normalise({"weird": {"nested": {"thing": "pool exhausted"}}, "n": 5})
    assert alert.source == UNKNOWN
    assert "pool exhausted" in alert.alert_text


def test_a_malformed_body_still_produces_something_triageable():
    assert normalise({"alerts": "not-a-list", "receiver": "x"}).alert_text.strip()


def test_non_dict_payloads_do_not_raise():
    assert normalise(["not", "a", "dict"]).source == UNKNOWN


def test_inbox_tracks_sources_and_grounding(tmp_path):
    inbox = Inbox(tmp_path / "inbox.json")
    inbox.add(IncomingAlert("text", ALERTMANAGER, service="a"), {"memory_grounded": True})
    inbox.add(IncomingAlert("text", DATADOG, service="b"), {"memory_grounded": False})
    stats = inbox.stats()
    assert stats == {
        "received": 2,
        "grounded": 1,
        "novel": 1,
        "by_source": {ALERTMANAGER: 1, DATADOG: 1},
    }


def test_inbox_keeps_only_the_most_recent(tmp_path):
    inbox = Inbox(tmp_path / "inbox.json", limit=3)
    for i in range(6):
        inbox.add(IncomingAlert(f"alert {i}", GENERIC), {})
    assert len(inbox.entries) == 3
    assert inbox.recent()[0]["alert"]["alert_text"] == "alert 5"


def test_inbox_survives_a_reload(tmp_path):
    path = tmp_path / "inbox.json"
    Inbox(path).add(IncomingAlert("persisted", GENERIC), {"memory_grounded": True})
    assert Inbox(path).stats()["received"] == 1


def test_a_body_with_nothing_extractable_yields_no_alert_text():
    """Not an imperfect alert — not an alert. The endpoint rejects it with 422."""
    assert normalise({}).alert_text == ""
    assert normalise({"a": None, "b": ""}).alert_text == ""
