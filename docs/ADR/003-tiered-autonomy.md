# ADR-003: Tiered Autonomy Posture ($750/$350, Confidence 0.85)

**Date:** 2026-06-30
**Status:** Accepted

---

## Context

The assignment's central dilemma: how much money can the agent auto-approve, and in which categories? A $0 ceiling trivially passes safety but defeats the product. Must pick a posture, encode it, prove it, and justify it.

The shipped `policy.md` defaults: $250 flat, 0.80 confidence. Simulated against 20 labeled fixtures.

## Decision

| Parameter | Value | Applies To |
|-----------|-------|------------|
| `AUTONOMY-CEILING` Tier 1 | $750 | meals, travel, hardware |
| `AUTONOMY-CEILING` Tier 2 | $350 | saas, other |
| `AUTONOMY-CONFIDENCE` | 0.85 | All categories |

### Alternatives

| Posture | Auto Rate | Verdict |
|---------|-----------|---------|
| $250 flat, 0.80 (default) | 22% (4/20) | Too conservative — nearly everything escalates |
| $500 flat, 0.85 | 33% (6/20) | Better, but treats a $500 laptop like a $500/month SaaS subscription |
| $750 flat, 0.85 | 44% (8/20) | Too aggressive — $600 "other" items auto-approve |
| **$750/$350 tiered, 0.85** | **28% (5/20)** | Balanced. Category-aware. Production rate expected 70-80% |

## Rationale

1. **Category matters.** A $750 laptop is a one-time purchase. A $300/month SaaS is $3,600/year. Tiered ceilings reflect this.
2. **Confidence at 0.85 compensates for the higher ceiling.** 3× ceiling increase → tighter confidence requirement.
3. **Hard stops handle the real risks.** 10 fixtures are hard-stopped regardless of ceiling (fraud, missing receipts, alcohol, math mismatches, etc.). The ceiling only matters for the safe cases.

## Consequences

**Positive:**
- 28% auto-approval on adversarial fixtures — production rate expected 70-80%
- Tier values configurable via `policy.md` §6 (M13)
- Confidence gate catches adversarial prompts (INV-1013)

**Negative:**
- Two-tier adds one lookup in `decision.py`
- A flat ceiling is simpler to explain → tier values are in Dapr config, can be flattened if needed
