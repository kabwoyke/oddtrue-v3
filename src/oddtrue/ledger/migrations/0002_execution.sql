-- Phase 3: persisted trade plan, loans, once-per-leg ledger entries, and the DONE/CANCELLED loan guard.

ALTER TABLE trades ADD COLUMN plan JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE trade_legs ADD COLUMN meta JSONB NOT NULL DEFAULT '{}'::jsonb;

-- DATA_MODEL `loans`: one row per loan a trade opened. verified_zero_at is set only after the EXCHANGE reports 0.
CREATE TABLE loans (
    id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    trade_id         TEXT NOT NULL REFERENCES trades (id),
    client_id        TEXT NOT NULL,
    exchange         TEXT NOT NULL,
    asset            TEXT NOT NULL,
    principal_before NUMERIC NOT NULL CHECK (principal_before >= 0),
    borrowed_qty     NUMERIC CHECK (borrowed_qty >= 0),
    verified_zero_at TIMESTAMPTZ,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (trade_id, exchange, asset)
);
CREATE INDEX loans_client ON loans (client_id, created_at);

-- A crash-and-resume must never book the same fill twice: one entry per (leg, kind).
CREATE UNIQUE INDEX ledger_entries_once_per_leg ON ledger_entries (ref_leg_id, kind) WHERE ref_leg_id IS NOT NULL;

-- DATA_MODEL invariant 2 / CLAUDE.md rule 6: a trade cannot be finished while any loan it opened is unverified.
CREATE FUNCTION require_loans_verified() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM loans WHERE trade_id = NEW.id AND verified_zero_at IS NULL) THEN
        RAISE EXCEPTION 'trade % has a loan not verified as zero', NEW.id USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER trades_finish_requires_zero_loans BEFORE UPDATE OF state ON trades
    FOR EACH ROW WHEN (NEW.state IN ('DONE', 'CANCELLED') AND OLD.state IS DISTINCT FROM NEW.state)
    EXECUTE FUNCTION require_loans_verified();
