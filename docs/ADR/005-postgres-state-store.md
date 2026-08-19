# ADR-005: PostgreSQL as State Store and Business Database

**Date:** 2026-06-30
**Status:** Accepted

---

## Context

Need: a Dapr state store (M5), a business database (F9 audit trail), idempotency guarantees (M10), transactional outbox (N3), and budget concurrency control (M9). Must be free, Docker Compose, no paid services.

## Decision

**PostgreSQL 16 — single instance serving both Dapr state store and business data.**

### Alternatives

| Criterion | PostgreSQL | Redis |
|-----------|-----------|-------|
| Dapr state store | ✅ | ✅ |
| Transactional outbox | ✅ Built-in | ❌ |
| Idempotency (relational constraints) | ✅ UNIQUE, FK | ❌ No constraints |
| `SELECT ... FOR UPDATE` | ✅ | ❌ |
| JSONB | ✅ | Native JSON |

## Rationale

**Transaction outbox is the decisive factor.** Dapr's PostgreSQL state store writes state + publishes events in one transaction. Without it, we'd build our own outbox (separate table, polling publisher, dedup) — which is N3 anyway.

**Redis rejected because:**
- No transactional outbox → write state OR publish event could fail alone
- No relational constraints → manual dedup logic for idempotency
- Business data is inherently relational → Redis would force denormalization

Business schemas (`ingestion`, `router`, `payment`, `notification`) are separate from Dapr's internal schema (`dapr_state`).

## Consequences

**Positive:**
- One database (not Postgres + Redis)
- Transactional outbox eliminates "wrote state but never published event" bug
- Relational constraints enforce idempotency at DB level
- `FOR UPDATE` for budget concurrency

**Negative:**
- Heavier than Redis (~150MB vs ~30MB)
- Single DB = single point of failure (acceptable for assignment)
- Requires schema migrations
