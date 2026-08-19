-- ApprovalFlow — Database Schema
-- Applied automatically by PostgreSQL on FIRST boot of an empty data volume.
-- After changing this file:  docker compose down -v && docker compose up -d
--
-- Storage split (see docs/ADR/005-postgres-state-store.md):
--   PostgreSQL     — business records needing transactions, joins and reporting:
--                    submissions, the transactional outbox, decisions, the
--                    approval queue, info requests, payment attempts.
--   Dapr state     — cross-cutting runtime state: the payment saga, department
--                    budget counters (ETag compare-and-set), the durable HITL
--                    pause token and the live policy configuration.
--   The dapr_state table itself is created and migrated by Dapr. Do not touch it.

-- ============================================
-- Ingestion Service
-- ============================================
CREATE SCHEMA IF NOT EXISTS ingestion;

CREATE TABLE IF NOT EXISTS ingestion.submissions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    correlation_id  UUID NOT NULL UNIQUE,
    submitter_email TEXT NOT NULL,
    department      TEXT NOT NULL,
    vendor          TEXT NOT NULL,
    vendor_known    BOOLEAN NOT NULL DEFAULT FALSE,
    invoice_number  TEXT NOT NULL,
    currency        TEXT NOT NULL DEFAULT 'USD',
    amount_original DECIMAL(14, 2) NOT NULL,
    amount_usd      DECIMAL(14, 2) NOT NULL,
    category        TEXT NOT NULL,
    attendees       INTEGER,
    line_items      JSONB NOT NULL DEFAULT '[]'::jsonb,
    tax_amount      DECIMAL(14, 2) NOT NULL DEFAULT 0,
    total           DECIMAL(14, 2) NOT NULL,
    receipt_present BOOLEAN NOT NULL DEFAULT FALSE,
    math_ok         BOOLEAN NOT NULL DEFAULT TRUE,
    raw_payload     JSONB NOT NULL,
    notes           TEXT,
    status          TEXT NOT NULL DEFAULT 'received',
    -- The plain-language outcome shown to the submitter (F2) and the rules
    -- behind it, denormalised from the decision event so the status endpoint is
    -- one indexed read rather than a cross-service call.
    decision_reason TEXT,
    rule_ids        TEXT[] NOT NULL DEFAULT '{}',
    -- Business idempotency key (vendor:invoiceNumber:total). NOT unique: a
    -- re-submission is stored so the submitter gets their own tracking id (F1),
    -- and is linked to the original through duplicate_of (F3).
    idempotency_key TEXT NOT NULL,
    duplicate_of    UUID REFERENCES ingestion.submissions(correlation_id),
    -- Bumped every time a send-back is answered, so each analysis round is
    -- separately auditable (F5 / F9).
    revision        INTEGER NOT NULL DEFAULT 0,
    -- The open "we need more information" request shown to the submitter, and
    -- the full append-only question/answer history.
    open_info_request JSONB,
    info_exchange   JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_submissions_correlation ON ingestion.submissions(correlation_id);
CREATE INDEX IF NOT EXISTS idx_submissions_business_key ON ingestion.submissions(vendor, invoice_number, total);
CREATE INDEX IF NOT EXISTS idx_submissions_idempotency ON ingestion.submissions(idempotency_key);
CREATE INDEX IF NOT EXISTS idx_submissions_status ON ingestion.submissions(status);
CREATE INDEX IF NOT EXISTS idx_submissions_created_at ON ingestion.submissions(created_at DESC);

-- Transactional outbox (N3). Rows are written in the SAME transaction as the
-- business row; a background dispatcher publishes them through Dapr pub/sub.
CREATE TABLE IF NOT EXISTS ingestion.outbox (
    id              BIGSERIAL PRIMARY KEY,
    correlation_id  UUID NOT NULL,
    topic           TEXT NOT NULL,
    payload         JSONB NOT NULL,
    -- pending -> published, or -> dead_letter after max attempts
    status          TEXT NOT NULL DEFAULT 'pending',
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    published_at    TIMESTAMPTZ
);

-- Partial index: the dispatcher's hot query only ever looks at pending rows.
CREATE INDEX IF NOT EXISTS idx_outbox_pending
    ON ingestion.outbox(next_attempt_at, id) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_outbox_correlation ON ingestion.outbox(correlation_id);

-- HTTP-level idempotency (M10): a client retrying POST /api/submissions with the
-- same Idempotency-Key header replays the original response instead of creating
-- a second submission.
CREATE TABLE IF NOT EXISTS ingestion.request_idempotency (
    idempotency_key TEXT PRIMARY KEY,
    correlation_id  UUID NOT NULL,
    response_body   JSONB NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Append-only status timeline — the "who did what, when" spine of the audit
-- trail (F9). Never updated, only inserted.
CREATE TABLE IF NOT EXISTS ingestion.submission_events (
    id              BIGSERIAL PRIMARY KEY,
    correlation_id  UUID NOT NULL,
    event_type      TEXT NOT NULL,
    from_status     TEXT,
    to_status       TEXT,
    actor           TEXT NOT NULL DEFAULT 'system',
    detail          JSONB NOT NULL DEFAULT '{}'::jsonb,
    occurred_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_submission_events_correlation
    ON ingestion.submission_events(correlation_id, occurred_at);

-- ============================================
-- Router Service
-- ============================================
CREATE SCHEMA IF NOT EXISTS router;

-- One row per decision round. Everything needed to reconstruct why an item went
-- where it went, and to prove the ceiling was never crossed (F9 / F10).
CREATE TABLE IF NOT EXISTS router.decisions (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    correlation_id          UUID NOT NULL,
    revision                INTEGER NOT NULL DEFAULT 0,
    -- Business identifiers, copied onto the decision so the router can answer
    -- reporting and re-publish a payment instruction after a human approval
    -- without reaching into another service's schema.
    invoice_number          TEXT,
    department              TEXT,
    -- What the agent said (advisory only)
    agent_recommendation    TEXT,
    agent_confidence        DECIMAL(4, 3),
    agent_violations        JSONB NOT NULL DEFAULT '[]'::jsonb,
    agent_reasoning         TEXT,
    agent_amount_usd        DECIMAL(14, 2),
    agent_category          TEXT,
    -- Retrieved policy clauses the recommendation was grounded in (N5 + F9)
    retrieved_rule_ids      TEXT[] NOT NULL DEFAULT '{}',
    -- What the router enforced
    submitted_amount_usd    DECIMAL(14, 2),
    enforced_amount_usd     DECIMAL(14, 2) NOT NULL,
    enforced_category       TEXT NOT NULL,
    ceiling_applied_usd     DECIMAL(14, 2) NOT NULL,
    confidence_threshold    DECIMAL(4, 3) NOT NULL,
    policy_config_version   INTEGER NOT NULL,
    rule_ids_applied        TEXT[] NOT NULL DEFAULT '{}',
    final_route             TEXT NOT NULL,
    decision_reason         TEXT,
    decided_by              TEXT NOT NULL DEFAULT 'router',
    decided_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_decision_round UNIQUE (correlation_id, revision, decided_by)
);

CREATE INDEX IF NOT EXISTS idx_decisions_correlation ON router.decisions(correlation_id);
CREATE INDEX IF NOT EXISTS idx_decisions_route ON router.decisions(final_route);
CREATE INDEX IF NOT EXISTS idx_decisions_decided_at ON router.decisions(decided_at DESC);

-- Router outbox: the decision row and the decision event are written in one
-- transaction, so a published event always has a persisted decision behind it
-- and a persisted decision always gets published (N3).
CREATE TABLE IF NOT EXISTS router.outbox (
    id              BIGSERIAL PRIMARY KEY,
    correlation_id  UUID NOT NULL,
    topic           TEXT NOT NULL,
    payload         JSONB NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    published_at    TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_router_outbox_pending
    ON router.outbox(next_attempt_at, id) WHERE status = 'pending';

-- Every accepted policy-configuration version, so "who loosened the ceiling and
-- when" is answerable (F7 audit + F10 context).
CREATE TABLE IF NOT EXISTS router.policy_versions (
    version     INTEGER PRIMARY KEY,
    document    JSONB NOT NULL,
    updated_by  TEXT NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ============================================
-- Payment Service
-- ============================================
CREATE SCHEMA IF NOT EXISTS payment;

-- Seeded budgets. The service copies these into the Dapr state store on first
-- start; the live counter afterwards is the state-store key (ETag CAS).
CREATE TABLE IF NOT EXISTS payment.department_budgets (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    department  TEXT NOT NULL UNIQUE,
    remaining   DECIMAL(14, 2) NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Audit record of every saga run, including compensation (F9 / M9).
CREATE TABLE IF NOT EXISTS payment.payment_attempts (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    correlation_id  UUID NOT NULL,
    saga_id         UUID NOT NULL,
    department      TEXT NOT NULL,
    amount_usd      DECIMAL(14, 2) NOT NULL,
    -- confirmed | compensated | failed
    outcome         TEXT NOT NULL,
    final_saga_state TEXT NOT NULL,
    steps           JSONB NOT NULL DEFAULT '[]'::jsonb,
    error_message   TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_payment_attempt UNIQUE (correlation_id, saga_id)
);

CREATE INDEX IF NOT EXISTS idx_payment_attempts_correlation
    ON payment.payment_attempts(correlation_id);

-- ============================================
-- Notification Service
-- ============================================
CREATE SCHEMA IF NOT EXISTS notification;

-- The approver queue (F4). Indexed so "show me only what was escalated" is a
-- single query rather than a scan plus N reads.
CREATE TABLE IF NOT EXISTS notification.approval_queue (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    correlation_id      UUID NOT NULL UNIQUE,
    -- pending | info_requested | approved | rejected
    status              TEXT NOT NULL DEFAULT 'pending',
    revision            INTEGER NOT NULL DEFAULT 0,
    vendor              TEXT,
    submitter_email     TEXT,
    department          TEXT,
    category            TEXT,
    amount_usd          DECIMAL(14, 2),
    -- Why the approver is looking at this: the agent's rationale and the rules
    -- the router cited (F4).
    agent_recommendation TEXT,
    agent_confidence    DECIMAL(4, 3),
    agent_reasoning     TEXT,
    rule_ids            TEXT[] NOT NULL DEFAULT '{}',
    escalation_reason   TEXT,
    -- What the submitter has answered so far, so the approver sees the thread.
    info_exchange       JSONB NOT NULL DEFAULT '[]'::jsonb,
    approver_email      TEXT,
    approver_action     TEXT,
    approver_comment    TEXT,
    paused_at           TIMESTAMPTZ,
    resumed_at          TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_approval_status ON notification.approval_queue(status, created_at);

-- "Send back for more info": exactly what was asked, by whom, and the answer
-- that resumed the workflow (F5).
CREATE TABLE IF NOT EXISTS notification.info_requests (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    correlation_id      UUID NOT NULL,
    revision            INTEGER NOT NULL DEFAULT 0,
    requested_by        TEXT NOT NULL,
    -- Structured "what we need" (e.g. ["receipt", "client_name"]) plus prose.
    requested_fields    TEXT[] NOT NULL DEFAULT '{}',
    question            TEXT NOT NULL,
    -- pending | answered | cancelled
    status              TEXT NOT NULL DEFAULT 'pending',
    answer              JSONB,
    answered_by         TEXT,
    requested_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    answered_at         TIMESTAMPTZ,
    CONSTRAINT uq_info_request_round UNIQUE (correlation_id, revision)
);

CREATE INDEX IF NOT EXISTS idx_info_requests_correlation
    ON notification.info_requests(correlation_id, requested_at);
CREATE INDEX IF NOT EXISTS idx_info_requests_status ON notification.info_requests(status);

-- Notification outbox: an approver's click is persisted and the resulting
-- approval.action_received event is enqueued in the same transaction, so a
-- pub/sub failure can never lose a human decision (N3 / M11).
CREATE TABLE IF NOT EXISTS notification.outbox (
    id              BIGSERIAL PRIMARY KEY,
    correlation_id  UUID NOT NULL,
    topic           TEXT NOT NULL,
    payload         JSONB NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    published_at    TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_notification_outbox_pending
    ON notification.outbox(next_attempt_at, id) WHERE status = 'pending';

-- Delivered notifications, so "the submitter was told" is auditable too (M8).
CREATE TABLE IF NOT EXISTS notification.notifications (
    id              BIGSERIAL PRIMARY KEY,
    correlation_id  UUID NOT NULL,
    channel         TEXT NOT NULL DEFAULT 'log',
    recipient       TEXT NOT NULL,
    subject         TEXT NOT NULL,
    body            TEXT NOT NULL,
    event_type      TEXT NOT NULL,
    sent_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_notifications_correlation
    ON notification.notifications(correlation_id, sent_at);

-- ============================================
-- Seed data
-- ============================================
INSERT INTO payment.department_budgets (department, remaining) VALUES
    ('marketing-2026Q2', 1000.00),
    ('engineering-2026Q2', 50000.00),
    ('sales-2026Q2', 20000.00)
ON CONFLICT (department) DO NOTHING;
