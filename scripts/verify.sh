#!/usr/bin/env bash
# Verify the whole project end to end: environment, database, lint, types, secret scan, tests, coverage, demo.
# Safe to run any time: it uses only the throwaway local Postgres from docker-compose.yml and the in-memory
# FakeExchange. It never touches real exchanges, real API keys or real funds.
#
#   scripts/verify.sh            full run (about 2-3 minutes, includes the crash-and-resume sweep)
#   scripts/verify.sh --quick    skip the crash-and-resume sweep (about 30 seconds)
#   scripts/verify.sh --keep-db  leave Postgres running afterwards (default: stop it only if this script started it)
#   scripts/verify.sh --live-public  also read PUBLIC Binance and Bitget data (no keys, no orders) through the read-only adapter
#   scripts/verify.sh --help
#
# Exit code 0 = everything passed. Non-zero = at least one check failed (see the summary at the end).

set -u -o pipefail

QUICK=0
KEEP_DB=0
LIVE_PUBLIC=0
for arg in "$@"; do
  case "$arg" in
    --quick) QUICK=1 ;;
    --keep-db) KEEP_DB=1 ;;
    --live-public) LIVE_PUBLIC=1 ;;
    -h | --help)
      sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      echo "unknown option: $arg (try --help)" >&2
      exit 2
      ;;
  esac
done

cd "$(dirname "$0")/.." || exit 2
ROOT="$(pwd)"
PY="$ROOT/.venv/bin/python"

if [ -t 1 ]; then
  RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; BOLD=$'\033[1m'; DIM=$'\033[2m'; RESET=$'\033[0m'
else
  RED=""; GREEN=""; YELLOW=""; BOLD=""; DIM=""; RESET=""
fi

LOG_DIR="$(mktemp -d)"
trap 'rm -rf "$LOG_DIR"' EXIT
RESULTS=()
FAILED=0
STARTED_DB=0

record() { # name status detail
  RESULTS+=("$1|$2|$3")
  [ "$2" = "FAIL" ] && FAILED=1
}

# run <name> <command...>: run quietly, show a one-line result, keep the log to print on failure.
run() {
  local name="$1"; shift
  local log="$LOG_DIR/$(echo "$name" | tr -c 'A-Za-z0-9' '_').log"
  printf '%s>>%s %s ... ' "$BOLD" "$RESET" "$name"
  local start=$SECONDS
  if "$@" >"$log" 2>&1; then
    printf '%sok%s %s(%ss)%s\n' "$GREEN" "$RESET" "$DIM" "$((SECONDS - start))" "$RESET"
    record "$name" PASS "$((SECONDS - start))s"
    return 0
  fi
  printf '%sFAILED%s\n' "$RED" "$RESET"
  echo "${DIM}---- last output of: $name ----${RESET}"
  tail -n 40 "$log"
  echo "${DIM}--------------------------------${RESET}"
  record "$name" FAIL "$((SECONDS - start))s"
  return 1
}

skip() { printf '%s>>%s %s ... %sskipped%s (%s)\n' "$BOLD" "$RESET" "$1" "$YELLOW" "$RESET" "$2"; record "$1" SKIP "$2"; }

cleanup() {
  if [ "$STARTED_DB" = 1 ] && [ "$KEEP_DB" = 0 ]; then
    echo "${DIM}stopping the Postgres container this script started (use --keep-db to leave it running)${RESET}"
    docker compose down >/dev/null 2>&1 || true
  fi
}
trap 'cleanup; rm -rf "$LOG_DIR"' EXIT

echo "${BOLD}Oddtrue verification${RESET}  ${DIM}($ROOT)${RESET}"
echo

# ---- 1. environment ----------------------------------------
if ! command -v python3 >/dev/null 2>&1; then
  echo "${RED}python3 not found${RESET}; install Python 3.12+ (see README, Prerequisites)" >&2
  exit 2
fi
run "python is 3.12 or newer" python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' || {
  echo "${RED}Python 3.12+ is required.${RESET} See README (Prerequisites)." >&2
  exit 1
}

if [ ! -x "$PY" ]; then
  run "create virtualenv (.venv)" python3 -m venv .venv || { echo "${YELLOW}hint: sudo apt install python3-venv${RESET}"; exit 1; }
fi
install_deps() {
  if "$PY" -m pip --version >/dev/null 2>&1; then
    "$PY" -m pip install -q -e ".[dev]"
  elif command -v uv >/dev/null 2>&1; then # a venv made by uv has no pip
    uv pip install -q -p "$PY" -e ".[dev]"
  else
    "$PY" -m ensurepip --upgrade >/dev/null 2>&1 && "$PY" -m pip install -q -e ".[dev]"
  fi
}
run "install project + dev tools" install_deps || exit 1

# ---- 2. database ----------------------------------------
DB_OK=0
if ! command -v docker >/dev/null 2>&1; then
  skip "start Postgres (docker compose)" "docker not installed; database tests will be skipped"
elif ! docker info >/dev/null 2>&1; then
  skip "start Postgres (docker compose)" "docker daemon not reachable (permission? try: sudo usermod -aG docker \$USER)"
else
  if docker compose ps --status running --services 2>/dev/null | grep -qx postgres; then
    echo "${BOLD}>>${RESET} start Postgres (docker compose) ... ${GREEN}already running${RESET}"
    record "start Postgres (docker compose)" PASS "already running"
    DB_OK=1
  elif run "start Postgres (docker compose)" docker compose up -d --wait; then
    STARTED_DB=1
    DB_OK=1
  fi
fi

if [ "$DB_OK" = 1 ]; then
  run "database schema migrates from scratch" "$PY" - <<'PY'
import asyncio, os
from oddtrue.ledger import db

DSN = os.environ.get("ODDTRUE_TEST_DATABASE_URL", "postgresql://oddtrue:oddtrue_dev@127.0.0.1:55432/oddtrue")

async def main() -> None:
    conn = await db.connect(DSN)
    await conn.execute("DROP SCHEMA public CASCADE")
    await conn.execute("CREATE SCHEMA public")
    applied = await db.migrate(conn)
    assert applied == [1, 2, 3], applied
    assert await db.migrate(conn) == []  # idempotent
    await conn.close()

asyncio.run(main())
PY
else
  echo "${YELLOW}Without Postgres the ledger, engine and crash tests cannot run, so this verification cannot pass.${RESET}"
  record "database available" FAIL "start Docker and rerun"
fi

# ---- 3. static checks ----------------------------------------
run "lint (ruff check)" "$PY" -m ruff check .
run "formatting (ruff format --check)" "$PY" -m ruff format --check .
run "strict type-check (mypy)" "$PY" -m mypy
# shellcheck disable=SC2046
run "secret scan (tracked + new files)" "$PY" scripts/scan_secrets.py $( (git ls-files; git ls-files -o --exclude-standard) | grep -v '^\.venv/' | sort -u)

# ---- 4. tests (+ coverage collected in the same run) ----------------------------------------
PURE="src/oddtrue/pricing/*,src/oddtrue/allocation/*,src/oddtrue/reconciliation/diff.py,src/oddtrue/reconciliation/expected.py,src/oddtrue/reconciliation/snapshot.py,src/oddtrue/exchanges/binance/depth.py,src/oddtrue/ratelimit/budget.py"
REST="src/oddtrue/exchanges/*,src/oddtrue/ledger/*,src/oddtrue/execution/*,src/oddtrue/recovery/*,src/oddtrue/reconciliation/*,src/oddtrue/safety/*,src/oddtrue/observability/*,src/oddtrue/ratelimit/*"
# Phase 5 exit criterion: no order/borrow/repay/withdraw endpoint is reachable from the real-exchange code. Needs no database.
run "Binance and Bitget adapters are read-only (guard + adapter tests)" "$PY" -m pytest -q tests/test_binance_readonly.py tests/test_bitget_readonly.py -p no:cacheprovider
run "no write verbs in real-exchange code" bash -c '! grep -rnEi "\.(post|put|delete|patch)\(|method *= *.?(POST|PUT|DELETE)" src/oddtrue/exchanges/binance src/oddtrue/exchanges/bitget --include=*.py'
if [ "$LIVE_PUBLIC" = 1 ]; then
  run "live public Binance read (no keys): book + symbol rules" bash -c "env -u BINANCE_API_KEY -u BINANCE_API_SECRET '$PY' scripts/exchange_readonly_smoke.py binance | grep -q '^rules:'"
  run "live public Bitget read (no keys): book + symbol rules" bash -c "env -u BITGET_API_KEY -u BITGET_API_SECRET -u BITGET_API_PASSPHRASE '$PY' scripts/exchange_readonly_smoke.py bitget | grep -q '^rules:'"
else
  skip "live public Binance and Bitget reads" "add --live-public to include (network, no keys)"
fi

if [ "$DB_OK" = 1 ]; then
  export CI=true # database tests must run, never silently skip
  rm -f .coverage
  if [ "$QUICK" = 1 ]; then
    run "test suite (without the crash sweeps)" "$PY" -m pytest -q --ignore=tests/test_engine_crash.py
    skip "crash-and-resume sweeps (~600 kill points)" "--quick"
    skip "coverage: 100% on pure math" "--quick"
    skip "coverage: >=98% on the rest" "--quick"
  else
    run "test suite (all, incl. crash-and-resume sweeps)" "$PY" -m pytest -q --cov=oddtrue --cov-branch
    # ---- 5. coverage gates (same thresholds as CI), read from the run above ----------------------------------------
    run "coverage: 100% on pure math (pricing, allocation, reconciliation maths)" "$PY" -m coverage report --include="$PURE" --fail-under=100
    run "coverage: >=98% on exchanges, ledger, engine, recovery, reconciliation, safety, observability" "$PY" -m coverage report --include="$REST" --fail-under=98
  fi
fi

# ---- 6. demo + safety defaults ----------------------------------------
run "fake-exchange demo ends with the loan verified at zero" bash -c "'$PY' scripts/demo_fake_cycle.py | tee '$LOG_DIR/demo.out' | grep -q 'outstanding loan now 0'"
run "default mode is paper; live is refused without its gates" "$PY" -c "
from pathlib import Path
from oddtrue.config import ConfigError, Mode, load_risk, resolve_mode
r = load_risk(Path('config/risk.yaml'))
assert resolve_mode(r, {}) is Mode.PAPER and not r.live_enabled and not r.live_allowlist
for env in ({'ODDTRUE_MODE': 'live'}, {'ODDTRUE_MODE': 'live', 'ODDTRUE_ALLOW_LIVE': '1'}):
    try:
        resolve_mode(r, env)
    except ConfigError:
        continue
    raise SystemExit(f'live was allowed with {env}')
"

# ---- summary ----------------------------------------
echo
echo "${BOLD}Summary${RESET}"
pass=0; fail=0; skipped=0
for row in "${RESULTS[@]}"; do
  IFS='|' read -r name status detail <<<"$row"
  case "$status" in
    PASS) mark="${GREEN}PASS${RESET}"; pass=$((pass + 1)) ;;
    FAIL) mark="${RED}FAIL${RESET}"; fail=$((fail + 1)) ;;
    *) mark="${YELLOW}SKIP${RESET}"; skipped=$((skipped + 1)) ;;
  esac
  printf '  %s  %-62s %s%s%s\n' "$mark" "$name" "$DIM" "$detail" "$RESET"
done
echo
if [ "$FAILED" = 0 ]; then
  echo "${GREEN}${BOLD}ALL CHECKS PASSED${RESET}  ($pass passed, $skipped skipped)"
  [ "$QUICK" = 1 ] && echo "${YELLOW}--quick skipped the crash sweep and coverage gates; run without it before approving a phase.${RESET}"
  exit 0
fi
echo "${RED}${BOLD}$fail CHECK(S) FAILED${RESET}  ($pass passed, $skipped skipped). Scroll up for the failing output."
exit 1
