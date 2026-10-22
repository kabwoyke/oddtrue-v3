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

Requires Python 3.12 or later. Run from the project root.

```powershell
# one-time setup (Windows PowerShell)
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
```

On macOS/Linux use `.venv/bin/python` instead of `.venv\Scripts\python`.

Checks (run all three before calling any task done):

```powershell
.venv\Scripts\python -m pytest -q              # tests (add --cov=oddtrue for coverage)
.venv\Scripts\python -m ruff check .           # lint
.venv\Scripts\python -m mypy                   # strict type-check
```

Secret scan (also runs in pre-commit and CI; prints file:line, never the value):

```powershell
.venv\Scripts\python scripts\scan_secrets.py <files...>
```

Optional: install the local git hook with `.venv\Scripts\python -m pre_commit install`.

Try the config loader and mode gating:

```powershell
.venv\Scripts\python -c "from pathlib import Path; from oddtrue.config import load_risk, resolve_mode; r = load_risk(Path('config/risk.yaml')); print(r.client_trade_pct, resolve_mode(r, {}))"
```

Configuration lives in `config/risk.yaml`, `config/scale.yaml` and `config/pairs.yaml`. Write decimals as quoted
strings (floats are rejected). Environment variable names are listed in `.env.example`.

There is no runnable engine yet: the project is built in phases (see `docs/PROGRESS.md`), and an engine that runs in
paper mode arrives in later phases. Nothing in the current code touches real exchanges or funds.

## Safety defaults

- Default mode is `paper`. Live trading needs an explicit env var, a config flag and a client allowlist.
- No real funds or real API keys are touched without the owner's explicit instruction.
- No secrets in code, tests, logs or commits.
