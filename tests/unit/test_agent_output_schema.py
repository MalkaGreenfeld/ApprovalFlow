"""Guards AGENT_OUTPUT_SCHEMA against drifting from the AgentOutput model.

The hand-written JSON Schema and the Pydantic model are two descriptions of the
same shape, so a field added to one and not the other has to fail here rather
than at run time when the provider rejects the request or a field arrives that
nothing reads.

Carried over from dev; the paths account for the schema being wrapped in the
Responses API envelope (``{"type": "json_schema", "schema": {...}}``).
"""

from __future__ import annotations

from approvalflow.models import AgentOutput
from services.agent.app.analyzer import AGENT_OUTPUT_SCHEMA

INNER = AGENT_OUTPUT_SCHEMA["schema"]


def test_schema_properties_match_model_fields():
    assert set(INNER["properties"]) == set(AgentOutput.model_fields)


def test_schema_required_matches_model_fields():
    # Strict mode requires every property to be listed as required.
    assert set(INNER["required"]) == set(AgentOutput.model_fields)


def test_the_schema_is_strict_and_closed():
    """Strict + additionalProperties=False is what makes the provider validate."""
    assert AGENT_OUTPUT_SCHEMA["strict"] is True
    assert INNER["additionalProperties"] is False
