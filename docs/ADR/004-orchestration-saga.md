# ADR-004: Orchestration-Based Saga for Payment Consistency

**Date:** 2026-06-30
**Status:** Accepted

---

## Context

Payment must guarantee a consistent outcome with compensation on failure (M9): no orphaned budget reservations, no partial payments, no double payments.

Three sequential steps: Reserve Budget → Execute Payment → Confirm. If any fails, prior steps must be undone.

## Decision

**Orchestration-based saga. Payment Service is the orchestrator.**

### Alternatives

| Approach | Why Rejected |
|----------|--------------|
| Choreography | Compensation logic spread across services — hard to reason about, hard to test ordering |
| Two-Phase Commit (2PC) | Payment gateway (external API) can't participate in a distributed transaction; blocking protocol |

## Rationale

1. **Sequential dependency maps naturally to an orchestrator.** Step 2 needs Step 1. Step 3 needs Step 2.
2. **Compensation is a mirror image.** Reverse steps in order: `compensate(S3, S2, S1)`.
3. **One file to read.** `saga.py` contains the entire flow — steps, success path, and compensation.
4. **Durable state.** `saga_state` + `current_step` written to PostgreSQL after each step. Survives crash.

### Failure Matrix

| Step that fails | S1 Compensate | S2 Compensate | S3 Compensate |
|-----------------|---------------|---------------|---------------|
| S1: Reserve | — | — | — |
| S2: Execute | Release reservation | — | — |
| S3: Confirm | Release reservation | Void payment | — |

## Consequences

**Positive:**
- Entire payment flow auditable in one file
- Saga state survives orchestrator crashes
- Easy to test: mock gateway, inject failure, verify compensation

**Negative:**
- Payment Service is single orchestration point (acceptable: stateless, all state in DB)
- New steps require modifying orchestrator (acceptable: 3 steps, stable)
