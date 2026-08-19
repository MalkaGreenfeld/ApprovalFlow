# ADR-002: Five-Service Decomposition

**Date:** 2026-06-30
**Status:** Accepted

---

## Context

M3 requires ≥3 containerized microservices. The system must: ingest asynchronously (M8), analyze with AI, make a deterministic decision the agent can't override (M12), process payment with compensation (M9), and manage an approver queue (M11).

The key question: should the AI agent and the deterministic router be one service or two?

## Decision

**5 services: Ingestion, Agent, Router, Payment, Notification.**

| # | Service | Responsibility | Why Separate |
|---|---------|---------------|--------------|
| 1 | Ingestion | Accept submissions, validate, assign correlation ID | Single entry point |
| 2 | Agent | AI analysis via LLM | Isolates all LLM code; swappable provider (M15) |
| 3 | Router | Deterministic decision | Contains NO AI code — cannot call an LLM (M12 proof) |
| 4 | Payment | Saga orchestration, budget management | Owns the payment transaction boundary (M9) |
| 5 | Notification | Deliver outcomes, approver queue | Owns HITL state (M11 — durable pause/resume) |

### Why NOT fewer services?

**3 services (merge Agent+Router, merge Payment+Notification):**
- Violates M12 — if Agent and Router share a process, a bug could bypass the ceiling check. Physical separation makes the proof trivial.
- Payment and Notification are different concerns — saga compensation shouldn't block notification delivery.

**4 services (merge Notification into another):**
- Notification owns the approver queue (M11) — mixing this with simple submission handling creates a bloated service.
- WebSocket delivery is its own infrastructure concern.

## Consequences

**Positive:**
- M12 is trivially provable: Router has no LLM imports
- Each service independently testable, deployable, scalable
- Service boundaries map to event flow: `submission.received → agent.analyzed → decision.* → payment.* → notification`

**Negative:**
- 5 Dockerfiles, 5 Dapr configs → mitigated: shared package, unified `docker compose up`
- Extra pub/sub hop (milliseconds, negligible vs. 1-3s LLM call)
