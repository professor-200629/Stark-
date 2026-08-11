"""
Shared test configuration.

Unit tests run against the **deterministic synthesis path** by default. Two
reasons, and the second matters more than the first:

  speed         the suite makes hundreds of triage calls. Against a live LLM that
                is nine minutes and a bill; deterministically it is eleven seconds.
  reproducible  an assertion like "the approved recommendation is ranked first"
                cannot be evaluated when the text under test is regenerated on
                every call. A flaky test that fails one run in five is worse than
                no test.

The LLM path is not left unchecked — `verify_demo.py` exercises it end to end with
whatever is configured, which is exactly how the two bugs this file's default would
have hidden were found. To run the suite against the LLM anyway:

    STARK_TEST_LLM=1 pytest -q
"""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True, scope="session")
def _deterministic_synthesis() -> None:
    """Force the deterministic path unless STARK_TEST_LLM is set."""
    if os.getenv("STARK_TEST_LLM") == "1":
        return

    from app import llm

    original = llm.available
    llm.available = lambda: False
    yield
    llm.available = original
