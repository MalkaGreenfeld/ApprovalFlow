"""RAG over the policy document (N5).

Retrieval has to be *better* than stuffing the whole policy in, not just
different, so these tests check three things: the right clause comes back for a
given item, hard stops are never dropped by a scoring accident, and the index
tracks the live document.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from services.agent.app.retrieval import (
    build_query,
    chunk_policy,
    format_clauses,
    get_index,
    reset_index,
    retrieved_rule_ids,
    tokenize,
)

POLICY = (Path(__file__).resolve().parents[2] / "policy.md").read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _fresh_index():
    reset_index()
    yield
    reset_index()


# ── Chunking ────────────────────────────────────────────────────────────────


def test_every_rule_becomes_its_own_retrievable_chunk():
    """One clause per chunk: retrieving MEAL-01 must not drag in all of section 1."""
    chunks = chunk_policy(POLICY)
    rule_ids = {c.rule_id for c in chunks if c.rule_id}

    for expected in (
        "MEAL-01", "MEAL-02", "MEAL-03",
        "TRAVEL-01", "TRAVEL-02", "TRAVEL-03",
        "SAAS-01", "HW-01", "HW-02",
        "GLOBAL-RECEIPT", "GLOBAL-VENDOR", "GLOBAL-FX",
        "GLOBAL-DUP", "GLOBAL-MATH", "GLOBAL-FRAUD",
    ):
        assert expected in rule_ids, f"{expected} was not indexed as its own clause"


def test_a_rule_chunk_carries_its_text_and_section():
    chunks = chunk_policy(POLICY)
    meal_01 = next(c for c in chunks if c.rule_id == "MEAL-01")

    assert "75" in meal_01.text
    assert "Meals" in meal_01.section
    assert meal_01.tokens


def test_table_scaffolding_is_not_indexed_as_content():
    """Separator rows would otherwise be retrievable noise."""
    chunks = chunk_policy(POLICY)
    assert not any(set(c.text) <= set("|-: ") for c in chunks)


def test_tokenize_drops_stop_words_and_lowercases():
    assert tokenize("The Receipt is REQUIRED for any Expense") == [
        "receipt",
        "required",
        "expense",
    ]


# ── Retrieval quality ───────────────────────────────────────────────────────


def test_a_saas_invoice_retrieves_the_saas_cap():
    index = get_index(POLICY, 1)
    submission = {
        "category": "saas",
        "vendor": "Atlassian",
        "vendor_known": True,
        "receipt_present": True,
        "amount_usd": 220,
        "currency": "USD",
        "math_ok": True,
        "line_items": [{"description": "Jira monthly subscription"}],
        "notes": "Recurring known SaaS",
    }

    results = index.retrieve(build_query(submission), category="saas")

    assert "SAAS-01" in retrieved_rule_ids(results)


def test_a_travel_invoice_retrieves_the_travel_clauses():
    index = get_index(POLICY, 1)
    submission = {
        "category": "travel",
        "vendor": "Lufthansa",
        "vendor_known": True,
        "receipt_present": True,
        "amount_usd": 1750,
        "currency": "USD",
        "math_ok": True,
        "line_items": [{"description": "Economy flight"}],
        "notes": "Single travel expense over the limit",
    }

    ids = retrieved_rule_ids(index.retrieve(build_query(submission), category="travel"))

    assert "TRAVEL-02" in ids


def test_hard_stops_are_always_retrieved_whatever_the_query():
    """Retrieval is an optimisation; it must never be why a hard stop went unseen.

    A nonsense query with no lexical overlap still has to surface every GLOBAL-*
    clause and the autonomy thresholds.
    """
    index = get_index(POLICY, 1)
    results = index.retrieve("qqqq zzzz nonsense", category="meals")
    ids = retrieved_rule_ids(results)

    for hard_stop in (
        "GLOBAL-RECEIPT", "GLOBAL-VENDOR", "GLOBAL-FX",
        "GLOBAL-DUP", "GLOBAL-MATH", "GLOBAL-FRAUD",
    ):
        assert hard_stop in ids, f"{hard_stop} was dropped by retrieval"


def test_mandatory_clauses_are_marked_as_always_applying():
    index = get_index(POLICY, 1)
    results = index.retrieve(build_query({"category": "meals"}), category="meals")

    mandatory = [chunk for chunk, score in results if score == math.inf]
    assert mandatory
    assert "ALWAYS APPLIES" in format_clauses(results)


def test_retrieval_is_a_subset_not_the_whole_document():
    """The entire point: the prompt gets the relevant clauses, not everything."""
    index = get_index(POLICY, 1)
    results = index.retrieve(build_query({"category": "saas"}), category="saas")

    assert len(results) < len(index.chunks), "retrieval returned the whole policy"
    rendered = format_clauses(results)
    assert len(rendered) < len(POLICY)


def test_an_unrelated_category_cap_is_not_retrieved_for_a_saas_item():
    """Confirms the retrieval is actually selective about category sections."""
    index = get_index(POLICY, 1)
    submission = {
        "category": "saas",
        "vendor": "Atlassian",
        "vendor_known": True,
        "receipt_present": True,
        "amount_usd": 99,
        "currency": "USD",
        "math_ok": True,
        "line_items": [{"description": "Jira monthly subscription"}],
        "notes": "",
    }

    ids = retrieved_rule_ids(index.retrieve(build_query(submission), category="saas"))

    assert "MEAL-01" not in ids


# ── Query construction ──────────────────────────────────────────────────────


def test_the_query_describes_the_facts_the_policy_is_written_about():
    query = build_query(
        {
            "category": "travel",
            "vendor": "Hotel Adler",
            "vendor_known": False,
            "receipt_present": False,
            "currency": "EUR",
            "math_ok": False,
            "amount_usd": 1296,
            "attendees": None,
            "line_items": [{"description": "Hotel, 3 nights"}],
            "notes": "Foreign currency",
        }
    ).lower()

    assert "travel" in query
    assert "new unknown vendor" in query
    assert "missing receipt" in query
    assert "currency conversion" in query
    assert "math does not reconcile" in query
    # No attendee wording for a hotel bill: the policy only discusses attendees
    # for meals, and putting it in every query pulled the meal per-attendee cap
    # into the prompt for unrelated items.
    assert "attendee" not in query


def test_the_query_mentions_a_missing_attendee_count_for_a_meal():
    query = build_query(
        {
            "category": "meals",
            "vendor": "The Rooftop Grill",
            "vendor_known": True,
            "receipt_present": True,
            "amount_usd": 620,
            "attendees": None,
            "line_items": [{"description": "Client dinner"}],
            "notes": "",
        }
    ).lower()

    assert "attendee count missing" in query


# ── Index lifecycle ─────────────────────────────────────────────────────────


def test_the_index_is_reused_for_an_unchanged_document():
    first = get_index(POLICY, 1)
    assert get_index(POLICY, 1) is first


def test_the_index_is_rebuilt_when_the_policy_changes():
    """Editing the policy through the admin API must change what is retrieved."""
    first = get_index(POLICY, 1)
    amended = POLICY + "\n| `SAAS-02` | Subscriptions over $5,000 need the CFO. |\n"

    second = get_index(amended, 2)

    assert second is not first
    assert second.digest != first.digest
    assert "SAAS-02" in {c.rule_id for c in second.chunks if c.rule_id}


def test_the_autonomy_ceiling_clause_is_indexed_despite_its_qualifier():
    """The autonomy table writes "`AUTONOMY-CEILING` Tier 1" in one cell.

    An id pattern that required the cell to end right after the id skipped both
    ceiling rows, so the single most important clause in the document was never
    retrievable.
    """
    chunks = chunk_policy(POLICY)
    ceiling_chunks = [c for c in chunks if c.rule_id == "AUTONOMY-CEILING"]

    assert len(ceiling_chunks) >= 2, "both ceiling tiers should be indexed"
    assert any("750" in c.text for c in ceiling_chunks)
    assert any("350" in c.text for c in ceiling_chunks)


def test_the_ceiling_clause_is_mandatory_for_every_item():
    """AUTONOMY-* clauses are always in the prompt, whatever the query scores."""
    index = get_index(POLICY, 1)
    ids = retrieved_rule_ids(index.retrieve("qqqq zzzz nonsense", category="saas"))

    assert "AUTONOMY-CEILING" in ids
    assert "AUTONOMY-CONFIDENCE" in ids
