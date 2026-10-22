-- Phase 2 ledger schema (Postgres, DECISIONS D-025).
-- Money, price and quantity columns are NUMERIC (exact decimal), never float. Timestamps are TIMESTAMPTZ.

CREATE FUNCTION reject_modification() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% is append-only', TG_TABLE_NAME USING ERRCODE = 'restrict_violation';
END;
$$;

CREATE TABLE trades (
    id              TEXT PRIMARY KEY,
    client_id       TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    buy_exchange    TEXT NOT NULL,
    borrow_exchange TEXT NOT NULL,
    starting_usdt   NUMERIC NOT NULL CHECK (starting_usdt >= 0),
    allocated_95    NUMERIC NOT NULL CHECK (allocated_95 >= 0),
    reserve_5       NUMERIC NOT NULL CHECK (reserve_5 >= 0),
    capital_used    NUMERIC NOT NULL CHECK (capital_used >= 0),
    state           TEXT NOT NULL,
    outcome         TEXT CHECK (outcome IN ('SUCCESS', 'RECOVERED', 'FAILED', 'CANCELLED')),
    started_at      TIMESTAMPTZ NOT NULL,
    finished_at     TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (buy_exchange <> borrow_exchange)
);
CREATE INDEX trades_client_created ON trades (client_id, created_at);

-- A leg row is written BEFORE the exchange call, so client_order_id is persisted first (CLAUDE.md rule 3).
CREATE TABLE trade_legs (
    id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    trade_id          TEXT NOT NULL REFERENCES trades (id),
    client_id         TEXT NOT NULL,
    leg               TEXT NOT NULL,
    attempt           INTEGER NOT NULL CHECK (attempt >= 1),
    client_order_id   TEXT NOT NULL UNIQUE,
    exchange_order_id TEXT,
    requested_qty     NUMERIC NOT NULL CHECK (requested_qty >= 0),
    filled_qty        NUMERIC,
    avg_price         NUMERIC,
    fee               NUMERIC,
    fee_asset         TEXT,
    status            TEXT NOT NULL CHECK (status IN
        ('INTENT', 'SENT', 'FILLED', 'PARTIALLY_FILLED', 'EXPIRED', 'REJECTED', 'UNKNOWN')),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (trade_id, leg, attempt)
);
CREATE INDEX trade_legs_trade ON trade_legs (trade_id);

-- Append-only: corrections are new ADJUSTMENT rows, never edits.
CREATE TABLE ledger_entries (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    client_id   TEXT NOT NULL,
    trade_id    TEXT REFERENCES trades (id),
    kind        TEXT NOT NULL CHECK (kind IN ('BUY_COST', 'SELL_PROCEEDS', 'FEE', 'INTEREST', 'SLIPPAGE',
        'RECOVERY_COST', 'TRANSFER_COST', 'REBALANCE_COST', 'ADJUSTMENT')),
    amount_usdt NUMERIC NOT NULL,
    asset       TEXT,
    qty         NUMERIC,
    ref_leg_id  BIGINT REFERENCES trade_legs (id),
    note        TEXT,
    at          TIMESTAMPTZ NOT NULL,
    -- every kind is a non-negative magnitude (the kind carries the sign) except ADJUSTMENT
    CHECK (kind = 'ADJUSTMENT' OR amount_usdt >= 0)
);
CREATE INDEX ledger_entries_client_at ON ledger_entries (client_id, at);
CREATE INDEX ledger_entries_trade ON ledger_entries (trade_id);

CREATE TABLE trade_state_log (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    trade_id   TEXT NOT NULL REFERENCES trades (id),
    from_state TEXT,
    to_state   TEXT NOT NULL,
    reason     TEXT,
    at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX trade_state_log_trade ON trade_state_log (trade_id, id);

-- Rotation audit (D-023): every include/skip with its reason. Last participation is derived from this table.
CREATE TABLE rotation_log (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    round_id       TEXT NOT NULL,
    opportunity_id TEXT NOT NULL,
    client_id      TEXT NOT NULL,
    position       INTEGER NOT NULL CHECK (position >= 1),
    qty            NUMERIC NOT NULL CHECK (qty >= 0),
    reason         TEXT NOT NULL,
    at             TIMESTAMPTZ NOT NULL,
    UNIQUE (round_id, client_id)
);
CREATE INDEX rotation_log_client_at ON rotation_log (client_id, at);

CREATE TRIGGER ledger_entries_append_only BEFORE UPDATE OR DELETE ON ledger_entries
    FOR EACH ROW EXECUTE FUNCTION reject_modification();
CREATE TRIGGER ledger_entries_no_truncate BEFORE TRUNCATE ON ledger_entries
    FOR EACH STATEMENT EXECUTE FUNCTION reject_modification();
CREATE TRIGGER trade_state_log_append_only BEFORE UPDATE OR DELETE ON trade_state_log
    FOR EACH ROW EXECUTE FUNCTION reject_modification();
CREATE TRIGGER trade_state_log_no_truncate BEFORE TRUNCATE ON trade_state_log
    FOR EACH STATEMENT EXECUTE FUNCTION reject_modification();
CREATE TRIGGER rotation_log_append_only BEFORE UPDATE OR DELETE ON rotation_log
    FOR EACH ROW EXECUTE FUNCTION reject_modification();
CREATE TRIGGER rotation_log_no_truncate BEFORE TRUNCATE ON rotation_log
    FOR EACH STATEMENT EXECUTE FUNCTION reject_modification();
