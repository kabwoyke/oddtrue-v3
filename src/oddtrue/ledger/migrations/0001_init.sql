-- Phase 2 ledger schema (SQLite dialect; see DECISIONS D-025 for the Postgres port).
-- Money/price/quantity columns are TEXT holding exact decimal strings: SQLite NUMERIC would coerce to float.

CREATE TABLE trades (
    id              TEXT PRIMARY KEY,
    client_id       TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    buy_exchange    TEXT NOT NULL,
    borrow_exchange TEXT NOT NULL,
    starting_usdt   TEXT NOT NULL,
    allocated_95    TEXT NOT NULL,
    reserve_5       TEXT NOT NULL,
    capital_used    TEXT NOT NULL,
    state           TEXT NOT NULL,
    outcome         TEXT,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    created_at      TEXT NOT NULL
);
CREATE INDEX trades_client_created ON trades (client_id, created_at);

-- A leg row is written BEFORE the exchange call, so client_order_id is persisted first (CLAUDE.md rule 3).
CREATE TABLE trade_legs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id          TEXT NOT NULL REFERENCES trades (id),
    client_id         TEXT NOT NULL,
    leg               TEXT NOT NULL,
    client_order_id   TEXT NOT NULL UNIQUE,
    exchange_order_id TEXT,
    requested_qty     TEXT NOT NULL,
    filled_qty        TEXT,
    avg_price         TEXT,
    fee               TEXT,
    fee_asset         TEXT,
    status            TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);
CREATE INDEX trade_legs_trade ON trade_legs (trade_id);

-- Append-only: corrections are new ADJUSTMENT rows, never edits.
CREATE TABLE ledger_entries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id   TEXT NOT NULL,
    trade_id    TEXT REFERENCES trades (id),
    kind        TEXT NOT NULL,
    amount_usdt TEXT NOT NULL,
    asset       TEXT,
    qty         TEXT,
    ref_leg_id  INTEGER REFERENCES trade_legs (id),
    note        TEXT,
    at          TEXT NOT NULL
);
CREATE INDEX ledger_entries_client_at ON ledger_entries (client_id, at);
CREATE INDEX ledger_entries_trade ON ledger_entries (trade_id);

CREATE TRIGGER ledger_entries_no_update BEFORE UPDATE ON ledger_entries
BEGIN SELECT RAISE(ABORT, 'ledger_entries is append-only'); END;
CREATE TRIGGER ledger_entries_no_delete BEFORE DELETE ON ledger_entries
BEGIN SELECT RAISE(ABORT, 'ledger_entries is append-only'); END;

CREATE TABLE trade_state_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id   TEXT NOT NULL REFERENCES trades (id),
    from_state TEXT,
    to_state   TEXT NOT NULL,
    reason     TEXT,
    at         TEXT NOT NULL
);
CREATE TRIGGER trade_state_log_no_update BEFORE UPDATE ON trade_state_log
BEGIN SELECT RAISE(ABORT, 'trade_state_log is append-only'); END;
CREATE TRIGGER trade_state_log_no_delete BEFORE DELETE ON trade_state_log
BEGIN SELECT RAISE(ABORT, 'trade_state_log is append-only'); END;

-- Rotation audit (D-023): every include/skip with its reason.
CREATE TABLE rotation_log (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id       TEXT NOT NULL,
    opportunity_id TEXT NOT NULL,
    client_id      TEXT NOT NULL,
    position       INTEGER NOT NULL,
    qty            TEXT NOT NULL,
    reason         TEXT NOT NULL,
    at             TEXT NOT NULL
);
CREATE INDEX rotation_log_client_at ON rotation_log (client_id, at);
CREATE TRIGGER rotation_log_no_update BEFORE UPDATE ON rotation_log
BEGIN SELECT RAISE(ABORT, 'rotation_log is append-only'); END;
CREATE TRIGGER rotation_log_no_delete BEFORE DELETE ON rotation_log
BEGIN SELECT RAISE(ABORT, 'rotation_log is append-only'); END;
