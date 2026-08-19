# ADR-001: Python + FastAPI

**Date:** 2026-06-30
**Status:** Accepted

---

## Context

Need a language for 5 microservices with Dapr, LLM integration, async event handling, structured logging. Must be free and run locally. Languages with first-class Dapr SDKs: Python, Node.js, C#, Java, Go.

## Decision

**Python 3.12+ with FastAPI.**

## Rationale

- **LLM ecosystem is best-in-class** — `openai` + `instructor` convert LLM output directly into Pydantic models. Other languages need manual JSON parsing.
- **FastAPI** provides native async, automatic OpenAPI spec (D4), and built-in health checks — all for free.
- **One language for all 5 services** enables a shared package (models, middleware, logging).
- **Dapr SDK** has full Python support (1.14+).

## Consequences

**Positive:**
- `instructor` + Pydantic prevents "agent returned malformed JSON" errors
- OpenAPI spec auto-generated (D4)
- Shared code across all services

**Negative:**
- Type safety is weaker than C#/Go → mitigated with `mypy` strict mode + Pydantic at every boundary
- Docker images heavier than Go (~150MB vs ~50MB) — not a concern here
