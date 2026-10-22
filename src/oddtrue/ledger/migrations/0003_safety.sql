-- Phase 4: audit log, kill switches, reconciliation records, client freezes, persisted alerts.

-- Every halt, resume, freeze, unfreeze and operator action is recorded here and can never be edited (CLAUDE.md rule 10).
CREATE TABLE audit_log (
    id        BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    actor     TEXT NOT NULL,
    client_id TEXT,
    action    TEXT NOT NULL,
    detail    JSONB NOT NULL DEFAULT '{}'::jsonb,
    at        TIMESTAMPTZ NOT NULL
);
CREATE INDEX audit_log_client_at ON audit_log (client_id, at);
CREATE INDEX audit_log_action_at ON audit_log (action, at);
CREATE TRIGGER audit_log_append_only BEFORE UPDATE OR DELETE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION reject_modification();
CREATE TRIGGER audit_log_no_truncate BEFORE TRUNCATE ON audit_log
    FOR EACH STATEMENT EXECUTE FUNCTION reject_modification();

-- Kill switches. A halt stops NEW trades only; in-flight trades finish or recover.
CREATE TABLE halts (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    scope        TEXT NOT NULL CHECK (scope IN ('GLOBAL', 'EXCHANGE', 'CLIENT')),
    target       TEXT,
    reason       TEXT NOT NULL CHECK (length(reason) > 0),
    set_by       TEXT NOT NULL,
    set_at       TIMESTAMPTZ NOT NULL,
    cleared_by   TEXT,
    cleared_at   TIMESTAMPTZ,
    clear_reason TEXT,
    CHECK ((scope = 'GLOBAL' AND target IS NULL) OR (scope <> 'GLOBAL' AND target IS NOT NULL)),
    CHECK ((cleared_at IS NULL) = (cleared_by IS NULL))
);
-- at most one active halt per (scope, target): halting twice is a no-op, not a second row to clear
CREATE UNIQUE INDEX halts_one_active ON halts (scope, COALESCE(target, '')) WHERE cleared_at IS NULL;

-- Each reconciliation of a client's exchange balances against what the ledger says they should be (DATA_MODEL).
-- BASELINE/OK/ACCEPTED rows are "known good" snapshots; the latest one is the starting point for the next diff.
CREATE TABLE reconciliations (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    client_id      TEXT NOT NULL,
    trigger        TEXT NOT NULL,
    status         TEXT NOT NULL CHECK (status IN ('BASELINE', 'OK', 'MISMATCH', 'ACCEPTED')),
    trade_id       TEXT,
    baseline_leg_id BIGINT NOT NULL,   -- legs up to this id are already reflected in `actual`/the starting snapshot
    expected       JSONB NOT NULL,
    actual         JSONB NOT NULL,
    diff           JSONB NOT NULL DEFAULT '[]'::jsonb,
    at             TIMESTAMPTZ NOT NULL,
    resolved_at    TIMESTAMPTZ,
    resolved_by    TEXT,
    note           TEXT
);
CREATE INDEX reconciliations_client ON reconciliations (client_id, id);

-- A frozen client (RECONCILING) accepts no new trades until a freeze is resolved. One active freeze per client.
CREATE TABLE client_freezes (
    id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    client_id         TEXT NOT NULL,
    reason            TEXT NOT NULL,
    reconciliation_id BIGINT REFERENCES reconciliations (id),
    trade_id          TEXT,
    frozen_at         TIMESTAMPTZ NOT NULL,
    resolved_at       TIMESTAMPTZ,
    resolved_by       TEXT,
    resolution        TEXT,
    CHECK ((resolved_at IS NULL) = (resolved_by IS NULL))
);
CREATE UNIQUE INDEX client_freezes_one_active ON client_freezes (client_id) WHERE resolved_at IS NULL;

CREATE TABLE alerts (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    severity        TEXT NOT NULL CHECK (severity IN ('PAGE', 'WARN')),
    kind            TEXT NOT NULL,
    client_id       TEXT,
    trade_id        TEXT,
    detail          TEXT NOT NULL,
    at              TIMESTAMPTZ NOT NULL,
    acknowledged_by TEXT,
    acknowledged_at TIMESTAMPTZ
);
CREATE INDEX alerts_kind_at ON alerts (kind, at);
CREATE INDEX alerts_client_at ON alerts (client_id, at);
