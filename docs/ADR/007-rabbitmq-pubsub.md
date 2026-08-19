# ADR-007: RabbitMQ as Pub/Sub Message Broker

**Date:** 2026-06-30
**Status:** Accepted

---

## Context

All inter-service communication is async pub/sub via Dapr (M5): service A publishes → broker delivers → service B processes and publishes next event.

Broker must support: Dapr, Docker Compose, free, persistent messages, dead-letter queues, survive restarts.

## Decision

**RabbitMQ 3.13.**

### Alternatives

| Criterion | RabbitMQ | Redis Pub/Sub | Kafka |
|-----------|----------|---------------|-------|
| Dapr support | ✅ | ✅ | ✅ |
| Message persistence | ✅ Durable queues | ❌ Fire-and-forget | ✅ |
| Dead-letter queues | ✅ Built-in (DLX) | ❌ | ⚠️ |
| Memory (Docker) | ~150MB | ~30MB | ~1GB |
| Complexity | Low | Very low | High |

**Redis rejected:** Messages lost on broker restart — a service restarting after crash misses events. Breaks M11 (durable HITL pause/resume).

**Kafka rejected:** Right for production at scale, but ~1GB RAM is disproportionate for 5 services with ~6 event types. Production swap requires only a Dapr component YAML change.

## Rationale

1. **Message persistence** — queues are durable. If Notification crashes, events accumulate and deliver on restart (M11).
2. **Dead-letter exchanges** — failed events after N retries route to DLX, not silently dropped.
3. **AMQP standard** — well-understood, mature Dapr component.
4. **Low operational overhead** — single container, management UI on port 15672.
5. **Dapr handles setup** — pub/sub topics map to exchanges, subscriptions map to queues automatically.

## Consequences

**Positive:**
- Messages survive broker restart
- Failed events visible via DLX
- Management UI for debugging
- Kafka swap = one YAML change in Dapr config

**Negative:**
- Heavier than Redis (~150MB vs ~30MB)
- AMQP debugging requires management UI or `rabbitmqctl`
