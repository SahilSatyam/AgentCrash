"""Inline pasted-NL store entries need no fixture file."""

from __future__ import annotations

import pytest

from agentcrash.adapter import load_nl_store


def test_inline_text_entry_needs_no_fixture() -> None:
    scenario = {
        "environment": {"nl_docs": [{"id": "pasted-request-1", "text": "Allow X to use Y."}]}
    }
    store = load_nl_store(scenario)
    entry = store["pasted-request-1"]
    assert entry["text"] == "Allow X to use Y."
    assert entry["label"] == "user_supplied"
    assert entry["fixture"] == "inline:pasted"
    assert entry["naive_odrl"] == {}
    assert entry["expected_odrl"] == {}


def test_inline_blank_text_refused() -> None:
    scenario = {"environment": {"nl_docs": [{"id": "pasted-request-1", "text": "  "}]}}
    with pytest.raises(ValueError):
        load_nl_store(scenario)


def test_fixture_entries_still_require_keys() -> None:
    scenario = {"environment": {"nl_docs": [{"id": "a", "fixture_key": "missing"}]}}
    with pytest.raises(ValueError):
        load_nl_store(scenario)
