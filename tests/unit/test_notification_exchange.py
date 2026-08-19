"""The information-exchange thread must survive a surprising shape.

A pub/sub handler that raises returns 500, Dapr redelivers, and the same event
fails forever while filling the log. That is what happened when the thread
arrived as JSON text instead of a list of objects: `exchange[-1]` was the last
*character* of the string, and `.get` on it raised AttributeError on every
redelivery.
"""

from __future__ import annotations

import json

from services.notification.app.main import _normalise_exchange

ENTRY = {
    "revision": 0,
    "request": {"question": "Which client?", "requested_fields": ["notes"]},
    "answer": {"answer": "Acme Corp", "updates": {"notes": "Client dinner, Acme Corp"}},
    "answered_by": "dana@northwind.example",
}


def test_a_proper_thread_passes_through():
    assert _normalise_exchange([ENTRY]) == [ENTRY]


def test_a_thread_encoded_as_json_text_is_parsed():
    """The shape that crashed the handler in a live run."""
    assert _normalise_exchange(json.dumps([ENTRY])) == [ENTRY]


def test_a_list_holding_the_encoded_thread_is_flattened():
    """A doubly-encoded write produced ["[{...}]"] in the database."""
    assert _normalise_exchange([json.dumps([ENTRY])]) == [ENTRY]


def test_a_list_of_encoded_entries_is_parsed():
    assert _normalise_exchange([json.dumps(ENTRY), json.dumps(ENTRY)]) == [ENTRY, ENTRY]


def test_a_nested_list_is_flattened():
    assert _normalise_exchange([[ENTRY, ENTRY]]) == [ENTRY, ENTRY]


def test_a_single_entry_is_wrapped():
    assert _normalise_exchange(ENTRY) == [ENTRY]


def test_absent_and_empty_read_as_no_thread():
    assert _normalise_exchange(None) == []
    assert _normalise_exchange([]) == []
    assert _normalise_exchange("") == []


def test_unusable_entries_are_dropped_rather_than_raising():
    """The handler must stay up: an unusable entry is skipped, not fatal."""
    assert _normalise_exchange(["not json at all"]) == []
    assert _normalise_exchange([ENTRY, 42, None]) == [ENTRY]
    assert _normalise_exchange(12345) == []


def test_the_answer_of_the_last_entry_is_reachable():
    """What the handler actually needs from the thread."""
    thread = _normalise_exchange([json.dumps([ENTRY])])

    assert thread[-1].get("answer", {})["answer"] == "Acme Corp"
