"""The externally configurable policy (F7 / M13) and its safety bounds (M12).

These tests are the counterpart to "the thresholds are configuration now": if
configuration can be changed at runtime, the interesting question is what it is
*not* allowed to say.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from approvalflow.policy import (
    ABSOLUTE_MAX_CEILING_USD,
    MIN_CONFIDENCE_THRESHOLD,
    PolicyConfigError,
    PolicyConfigStore,
    RuleOutcome,
    default_policy_config,
    parse_policy_config,
)


def test_bootstrap_document_loads_and_matches_the_documented_posture(policy_config):
    """The shipped configuration is the posture described in policy.md section 6."""
    assert policy_config.autonomy.confidence_threshold == Decimal("0.85")
    assert policy_config.autonomy.ceiling_for("meals") == Decimal("750")
    assert policy_config.autonomy.ceiling_for("travel") == Decimal("750")
    assert policy_config.autonomy.ceiling_for("hardware") == Decimal("750")
    assert policy_config.autonomy.ceiling_for("saas") == Decimal("350")
    assert policy_config.autonomy.ceiling_for("other") == Decimal("350")


def test_unknown_category_falls_back_to_the_default_ceiling(policy_config):
    """A category nobody configured must not get unlimited autonomy."""
    assert policy_config.autonomy.ceiling_for("spacecraft") == Decimal("350")


def test_configuration_cannot_express_auto_approve():
    """The strongest guarantee: configuration can restrict autonomy, never grant it.

    ``RuleOutcome`` has no ``auto_approve`` member, so there is no edit to the
    policy document that turns an escalation into an automatic approval.
    """
    assert {o.value for o in RuleOutcome} == {"human_review", "reject", "duplicate"}

    with pytest.raises(PolicyConfigError):
        parse_policy_config(
            {
                "rules": [
                    {
                        "rule_id": "EVIL-01",
                        "outcome": "auto_approve",
                        "when": {"field": "amount_usd", "op": "gt", "value": "0"},
                    }
                ]
            }
        )


def test_ceiling_above_the_compiled_maximum_is_rejected():
    """No configuration may raise a ceiling past the router's hard limit."""
    document = default_policy_config().to_json_dict()
    document["autonomy"]["category_ceilings_usd"]["meals"] = str(
        ABSOLUTE_MAX_CEILING_USD + 1
    )

    with pytest.raises(PolicyConfigError, match="hard maximum"):
        parse_policy_config(document)


def test_default_ceiling_above_the_compiled_maximum_is_rejected():
    """The fallback ceiling is bounded too, otherwise it would be the loophole."""
    document = default_policy_config().to_json_dict()
    document["autonomy"]["default_ceiling_usd"] = "999999"

    with pytest.raises(PolicyConfigError, match="hard maximum"):
        parse_policy_config(document)


def test_confidence_bar_cannot_be_dropped_below_the_floor():
    document = default_policy_config().to_json_dict()
    document["autonomy"]["confidence_threshold"] = str(MIN_CONFIDENCE_THRESHOLD / 2)

    with pytest.raises(PolicyConfigError, match="confidence_threshold"):
        parse_policy_config(document)


def test_negative_ceiling_is_rejected():
    document = default_policy_config().to_json_dict()
    document["autonomy"]["category_ceilings_usd"]["meals"] = "-100"

    with pytest.raises(PolicyConfigError, match="negative"):
        parse_policy_config(document)


def test_base_currency_must_convert_one_to_one():
    """An FX rate of 1.5 on USD would silently inflate every amount."""
    document = default_policy_config().to_json_dict()
    document["fx_rates"]["USD"] = "1.5"

    with pytest.raises(PolicyConfigError, match="base currency"):
        parse_policy_config(document)


def test_duplicate_rule_ids_are_rejected():
    """Two rules with one id would make the audit trail ambiguous."""
    document = default_policy_config().to_json_dict()
    document["rules"].append(dict(document["rules"][0]))

    with pytest.raises(PolicyConfigError, match="duplicate rule_id"):
        parse_policy_config(document)


def test_vendor_known_is_not_amendable_by_a_submitter(policy_config):
    """A submitter must not be able to assert that a vendor is known.

    That claim is what the GLOBAL-VENDOR hard stop is checking, so letting the
    submitter set it would let them switch off their own review.
    """
    assert "vendorKnown" not in policy_config.amendable_fields
    assert "vendor" not in policy_config.amendable_fields


# ── The live store ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_store_seeds_the_bootstrap_document_on_first_read(fake_state):
    """With nothing stored, the first read seeds the state store and returns it."""
    store = PolicyConfigStore(fake_state, ttl_seconds=0)
    config = await store.get()

    assert config.version == 1
    assert await fake_state.get("config:policy") is not None


@pytest.mark.asyncio
async def test_saving_bumps_the_version_and_records_the_author(fake_state):
    """The version is assigned by the service, so history cannot be rewritten."""
    store = PolicyConfigStore(fake_state, ttl_seconds=0)
    original = await store.get()

    document = original.to_json_dict()
    document["version"] = 999  # a caller trying to choose its own version
    document["autonomy"]["category_ceilings_usd"]["meals"] = "500"

    saved = await store.save(document, updated_by="controller@northwind.example")

    assert saved.version == original.version + 1
    assert saved.updated_by == "controller@northwind.example"
    assert saved.autonomy.ceiling_for("meals") == Decimal("500")


@pytest.mark.asyncio
async def test_a_change_is_visible_to_another_replica_without_a_restart(fake_state):
    """F7/M13: one replica writes, another sees it once its short cache expires."""
    writer = PolicyConfigStore(fake_state, ttl_seconds=0)
    reader = PolicyConfigStore(fake_state, ttl_seconds=0)

    assert (await reader.get()).autonomy.ceiling_for("saas") == Decimal("350")

    document = (await writer.get()).to_json_dict()
    document["autonomy"]["category_ceilings_usd"]["saas"] = "100"
    await writer.save(document, updated_by="controller@northwind.example")

    assert (await reader.get()).autonomy.ceiling_for("saas") == Decimal("100")


@pytest.mark.asyncio
async def test_an_unsafe_update_is_refused_and_the_live_copy_is_untouched(fake_state):
    """A rejected update must not partially apply."""
    store = PolicyConfigStore(fake_state, ttl_seconds=0)
    before = await store.get()

    document = before.to_json_dict()
    document["autonomy"]["category_ceilings_usd"]["meals"] = "50000"

    with pytest.raises(PolicyConfigError):
        await store.save(document, updated_by="attacker@example.com")

    store.invalidate()
    after = await store.get()
    assert after.autonomy.ceiling_for("meals") == before.autonomy.ceiling_for("meals")
    assert after.version == before.version


@pytest.mark.asyncio
async def test_a_corrupt_stored_document_does_not_disable_the_guardrails(fake_state):
    """If the stored copy is unreadable, fall back to a known-good one loudly.

    The dangerous failure would be an empty policy: no rules, so nothing to
    violate, so everything auto-approves. This asserts that cannot happen.
    """
    await fake_state.save("config:policy", {"autonomy": {"confidence_threshold": "-5"}})
    store = PolicyConfigStore(fake_state, ttl_seconds=0)

    config = await store.get()

    assert config.autonomy.confidence_threshold >= MIN_CONFIDENCE_THRESHOLD
    assert config.rules, "fell back to a policy with no rules"
