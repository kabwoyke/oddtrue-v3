#!/usr/bin/env bash
# Owner-run live READ-ONLY check of both exchanges, in one go (never part of CI, never places an order).
#
#   scripts/live_check.sh                 Binance + Bitget smoke tests, then 60 s of live detection
#   scripts/live_check.sh --seconds 120   longer detection run
#   scripts/live_check.sh --no-detect     smoke tests only
#   scripts/live_check.sh --help
#
# Keys come from .env (git-ignored; see .env.example). Use keys with READ permission only, withdrawals OFF.
# Without keys the smoke tests still read public data and skip the signed reads; detection then needs
# --assume-taker-fee and --assume-borrow-rate-hourly (passed through below) because fees and rates are signed reads.
# Exit code 0 = every step ran; non-zero = a step failed (the summary says which).

set -u -o pipefail

SECONDS_RUN=60
DETECT=1
for arg in "$@"; do
  case "$arg" in
    --no-detect) DETECT=0 ;;
    --seconds) shift_next=1 ;;
    -h | --help) sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *)
      if [ "${shift_next:-0}" = 1 ]; then SECONDS_RUN="$arg"; shift_next=0
      else echo "unknown option: $arg (try --help)" >&2; exit 2; fi ;;
  esac
done

cd "$(dirname "$0")/.." || exit 2
PY=".venv/bin/python"
[ -x "$PY" ] || { echo "no .venv: run scripts/verify.sh first" >&2; exit 2; }
if [ -t 1 ]; then B=$'\033[1m'; G=$'\033[32m'; R=$'\033[31m'; Y=$'\033[33m'; X=$'\033[0m'; else B=""; G=""; R=""; Y=""; X=""; fi

# Are keys filled in? (names only: values are never printed)
filled() { grep -Eq "^$1=.+" .env 2>/dev/null; }
HAVE_BINANCE=0; HAVE_BITGET=0
filled BINANCE_API_KEY && filled BINANCE_API_SECRET && HAVE_BINANCE=1
filled BITGET_API_KEY && filled BITGET_API_SECRET && filled BITGET_API_PASSPHRASE && HAVE_BITGET=1
echo "${B}Oddtrue live read-only check${X}  (binance keys: $([ $HAVE_BINANCE = 1 ] && echo yes || echo no), bitget keys: $([ $HAVE_BITGET = 1 ] && echo yes || echo no))"

FAILED=()
step() { # name command...
  local name="$1"; shift
  echo; echo "${B}=== $name ===${X}"
  if "$@"; then echo "${G}ok${X}: $name"; else echo "${R}FAILED${X}: $name"; FAILED+=("$name"); fi
}

step "Binance smoke test" "$PY" scripts/exchange_readonly_smoke.py binance
step "Bitget smoke test" "$PY" scripts/exchange_readonly_smoke.py bitget

if [ "$DETECT" = 1 ]; then
  if [ $HAVE_BINANCE = 1 ] && [ $HAVE_BITGET = 1 ]; then
    step "Live detection (${SECONDS_RUN}s, costs read from the exchanges)" "$PY" scripts/detect_live.py --seconds "$SECONDS_RUN" --quiet
  else
    echo "${Y}No full key set: detection uses explicit ASSUMED costs (fee 0.001, borrow 0.00001/h), clearly marked.${X}"
    step "Live detection (${SECONDS_RUN}s, ASSUMED costs)" "$PY" scripts/detect_live.py --seconds "$SECONDS_RUN" --quiet \
      --assume-taker-fee 0.001 --assume-borrow-rate-hourly 0.00001
  fi
fi

echo; echo "${B}Summary${X}"
if [ ${#FAILED[@]} -eq 0 ]; then echo "${G}all steps ran${X} (a signed read that printed 'Rejected ...' still counts as a run: read those lines)"; exit 0; fi
for f in "${FAILED[@]}"; do echo "  ${R}FAILED${X}  $f"; done
exit 1
