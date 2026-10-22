# Oddtrue

Multi-client cross-exchange crypto arbitrage engine (first exchanges: **Binance** and **Bitget**) with a paid
(M-Pesa) subscription, user-supplied API keys, and a per-client dashboard.

Clients keep their funds on their own exchange accounts. Oddtrue trades on their behalf through API keys
(trade/margin permissions only, no withdrawals unless an authorized automatic-transfer feature requires it).

## Read these first (in order)

| File | Purpose |
|---|---|
| `CLAUDE.md` | Rules, architecture, phased build plan, acceptance scenarios. **Start here.** |
| `docs/ODDTRUE_SPEC.md` | The 62-section engine + user-flow specification (source of truth). |
| `docs/USER_JOURNEY.md` | Worked end-to-end example ("John", Binance + Bitget). Acceptance reference. |
| `docs/DECISIONS.md` | Open and settled decisions. Update whenever something is decided. |
| `docs/PROGRESS.md` | Current phase and status. Update at the end of every work session. |
| `docs/DATA_MODEL.md` | Initial database design. |
| `docs/SECURITY.md` | Threat model and security requirements. |
| `docs/RUNBOOKS.md` | Operational procedures (incidents, halts, stuck loans). |
| `docs/exchanges/binance.md` | Verified Binance API findings (fill in during Phase 5). |
| `docs/exchanges/bitget.md` | Verified Bitget API findings (fill in during Phase 5). |
| `docs/exchanges/mpesa.md` | Verified M-Pesa Daraja findings (fill in during Phase 8d). |

## Status

See `docs/PROGRESS.md`. Work happens in gated phases (0–10); never start a phase before the previous one is approved.

## Setup, test and run

There is no runnable engine yet (the project is built in gated phases, see `docs/PROGRESS.md`). What you can run today
is the test suite, the checks, and a demo of the fake exchange. Nothing here touches real exchanges, real API keys or
real funds.

Commands below are for Linux (written for Linux Mint 22 / Ubuntu 24.04) and run from the project root.

### Prerequisites

- Python 3.12 or later (`python3 --version`). Mint 22 ships 3.12; Mint 21 ships 3.10, which is too old (install 3.12
  with uv: `uv venv .venv --python 3.12`, or from the deadsnakes PPA).
- The venv module: `sudo apt install python3-venv python3-pip` (Mint does not always include it)
- Docker with the Compose plugin (`docker --version`, `docker compose version`). If missing:
  `sudo apt install docker.io docker-compose-v2`
- Docker without sudo: `sudo usermod -aG docker $USER`, then log out and back in
- Optional: [uv](https://docs.astral.sh/uv/) (faster installs); plain `python3 -m venv` + `pip` works too

### One command: verify everything

```bash
scripts/verify.sh            # full run, about 4 minutes (the sweeps kill the engine at ~600 points and resume it)
scripts/verify.sh --quick    # skips the crash-and-resume sweep and coverage gates, about 30 seconds
scripts/verify.sh --keep-db  # leave Postgres running afterwards
```

It creates the virtualenv if needed, installs the project, starts the throwaway Postgres (and stops it again if it
started it), then checks: the schema migrates from scratch, lint, formatting, strict types, the secret scan, the whole
test suite (including the crash-and-resume sweep), the coverage gates CI enforces, the fake-exchange demo, and that the
default mode is paper and live is refused without its gates. It prints a PASS/FAIL/SKIP summary and exits 0 only if
everything passed, so it is safe to use in scripts. Run the full version before approving a phase. The steps below do
the same things by hand.

### Step 1: create the virtual environment and install

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
```

With uv: `uv venv .venv --python 3.12 && uv pip install -p .venv/bin/python -e ".[dev]"`

### Step 2: start the local Postgres (needed by the ledger tests)

```bash
docker compose up -d --wait
```

This starts a throwaway Postgres 16 on `127.0.0.1:55432` (user/password/database are dev-only values in
`docker-compose.yml`; data lives in memory and is gone when the container stops). If port 55432 is busy, change it in
`docker-compose.yml` and set `ODDTRUE_TEST_DATABASE_URL` to match, for example
`postgresql://oddtrue:oddtrue_dev@127.0.0.1:<port>/oddtrue`.

Check it is healthy: `docker compose ps` should show `healthy`.

### Step 3: run the tests

```bash
.venv/bin/python -m pytest -q
```

Expect all tests to pass (336 at the end of Phase 4; the crash-and-resume sweeps take about two minutes). If Postgres is not running, the database tests are **skipped**
(shown as `s` in the output); start it with Step 2 and rerun. In CI they must run and never skip.

Useful variations:

```bash
.venv/bin/python -m pytest tests/test_ledger.py -q            # one file
.venv/bin/python -m pytest -q -k recovered                    # tests matching a name
.venv/bin/python -m pytest -q --cov=oddtrue --cov-branch --cov-report=term-missing   # coverage
```

### Step 4: run the checks (all three must pass before any task is "done")

```bash
.venv/bin/python -m ruff check .       # lint
.venv/bin/python -m ruff format --check .   # formatting
.venv/bin/python -m mypy               # strict type-check
```

### Step 5: try the fake exchange demo

```bash
.venv/bin/python scripts/demo_fake_cycle.py
```

It runs one borrow, protected sell, buy back and repay cycle on the in-memory `FakeExchange` and prints the result,
ending with the loan verified at zero. It needs no database and no network.

### Step 6: inspect the ledger database (optional)

```bash
docker compose exec postgres psql -U oddtrue -d oddtrue
```

The test suite recreates the schema for every test, so tables only exist while a test is running. To explore by hand,
apply the migration yourself:

```bash
docker compose exec -T postgres psql -U oddtrue -d oddtrue < src/oddtrue/ledger/migrations/0001_init.sql
```

This hand-applied schema is for browsing only: it does not record the migration in `schema_migrations`. Do not run
the app's migrator against it; reset first with `docker compose down && docker compose up -d --wait`.

### Step 7: stop everything

```bash
docker compose down
```

### Other tools

Secret scan (also runs in pre-commit and CI; prints file:line, never the value):

```bash
.venv/bin/python scripts/scan_secrets.py $(git ls-files)
```

Install the local git hook (runs the secret scan on commit): `.venv/bin/python -m pre_commit install`

Try the config loader and mode gating:

```bash
.venv/bin/python -c "from pathlib import Path; from oddtrue.config import load_risk, resolve_mode; r = load_risk(Path('config/risk.yaml')); print(r.client_trade_pct, resolve_mode(r, {}))"
```

### Configuration

Thresholds live in `config/risk.yaml`, `config/scale.yaml` and `config/pairs.yaml`. Write decimals as quoted strings
(floats are rejected). Environment variable names are listed in `.env.example` (names only, never real values).
Numeric limits in `risk.yaml` are provisional placeholders until the owner sets them (DECISIONS #12).

### Troubleshooting

| Symptom | Fix |
|---|---|
| DB tests show as skipped | Postgres is not running or not reachable: `docker compose up -d --wait` |
| `port is already allocated` on 55432 | Change the host port in `docker-compose.yml` and set `ODDTRUE_TEST_DATABASE_URL` |
| `connection refused` / auth failure | `docker compose ps`; recreate with `docker compose down && docker compose up -d --wait` |
| `ModuleNotFoundError: oddtrue` | Rerun Step 1 (editable install) and use `.venv/bin/python` |
| `permission denied` talking to Docker | `sudo usermod -aG docker $USER`, then log out and back in (or prefix with `sudo`) |
| `ensurepip is not available` creating the venv | `sudo apt install python3-venv` |
| `externally-managed-environment` from pip | Use the venv (`.venv/bin/python -m pip ...`), never system pip |

## Safety defaults

- Default mode is `paper`. Live trading needs an explicit env var, a config flag and a client allowlist.
- No real funds or real API keys are touched without the owner's explicit instruction.
- No secrets in code, tests, logs or commits.
