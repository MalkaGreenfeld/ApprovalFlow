# ADR-006: Physical Agent-Router Separation (M12 Proof)

**Date:** 2026-06-30
**Status:** Accepted

---

## Context

M12: the system must be "provably incapable" of auto-approving above the configured ceiling — even when the agent is forced to recommend approval (hallucination, prompt injection, crafted input).

How do we architecturally guarantee the ceiling is always enforced?

## Decision

**Agent and Router are separate services — separate containers, separate codebases, separate Dapr app IDs.**

Router has zero AI code. No LLM imports. No HTTP calls to AI APIs. The ceiling check is a plain Python `if` statement:

```python
def decide(agent_output, submission):
    # ... hard stops ...
    ceiling = TIER_CEILINGS[submission.category]
    if submission.amount_usd > ceiling:
        return escalate("ceiling")
    if agent_output.confidence < 0.85:
        return escalate("low_confidence")
    return auto_approve()
```

### Alternatives

| Approach | Why Rejected |
|----------|--------------|
| Same service, different modules | Proof relies on code review, not architecture. A bug could bypass router. |
| Router as a library imported by Agent | Agent process owns the decision — M12 requires the opposite |
| Router as Dapr middleware | Only works for HTTP/gRPC, not pub/sub messages |

## Rationale

**Physical separation makes the proof trivial.** A reviewer opens Router's `requirements.txt` — sees no `openai`, `anthropic`, `instructor`. Opens `decision.py` — sees a deterministic function. The proof: "this service cannot call an LLM, therefore no LLM can manipulate it into bypassing its own ceiling check."

Same principle as a hardware security module — the critical component runs isolated from the untrusted system (the LLM). The agent is untrusted by design: it produces a recommendation the router treats as advisory input.

## Consequences

**Positive:**
- M12 proof is architectural, not just procedural
- Router testable independently: 100% deterministic coverage possible
- Security audit needs to review only one file: `decision.py`

**Negative:**
- One extra service (5 vs 4)
- Extra pub/sub hop → milliseconds, negligible vs 1-3s LLM call
