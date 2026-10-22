"""A tiny in-process metrics registry (counters, gauges, histograms) with Prometheus text output.

No external dependency and no I/O: the process exposes ``render()`` over whatever endpoint it likes. Label values are
kept low-cardinality by callers (exchange, outcome, leg, kind) - never trade or client ids.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

Labels = tuple[tuple[str, str], ...]

DEFAULT_BUCKETS = (0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0)


def _labels(raw: dict[str, str]) -> Labels:
    return tuple(sorted(raw.items()))


def _fmt(labels: Labels, extra: str = "") -> str:
    parts = [f'{k}="{v}"' for k, v in labels] + ([extra] if extra else [])
    return "{" + ",".join(parts) + "}" if parts else ""


@dataclass(slots=True)
class _Hist:
    buckets: tuple[float, ...]
    counts: list[int] = field(default_factory=list)
    total: float = 0.0
    n: int = 0

    def observe(self, v: float) -> None:
        if not self.counts:
            self.counts = [0] * len(self.buckets)
        for i, b in enumerate(self.buckets):
            if v <= b:
                self.counts[i] += 1
        self.total += v
        self.n += 1


class Metrics:
    def __init__(self) -> None:
        self._counters: dict[str, dict[Labels, Decimal]] = defaultdict(dict)
        self._gauges: dict[str, dict[Labels, Decimal]] = defaultdict(dict)
        self._hists: dict[str, dict[Labels, _Hist]] = defaultdict(dict)
        self._help: dict[str, str] = {}

    def describe(self, name: str, text: str) -> None:
        self._help[name] = text

    def inc(self, name: str, value: Decimal | int = 1, **labels: str) -> None:
        if value < 0:
            raise ValueError("a counter can only go up")
        key = _labels(labels)
        self._counters[name][key] = self._counters[name].get(key, Decimal(0)) + Decimal(value)

    def set(self, name: str, value: Decimal | int, **labels: str) -> None:
        self._gauges[name][_labels(labels)] = Decimal(value)

    def clear_gauge(self, name: str) -> None:
        self._gauges[name].clear()

    def observe(self, name: str, seconds: float, buckets: tuple[float, ...] = DEFAULT_BUCKETS, **labels: str) -> None:
        key = _labels(labels)
        hist = self._hists[name].get(key)
        if hist is None:
            hist = self._hists[name][key] = _Hist(buckets)
        hist.observe(seconds)

    # ---- reading (tests, dashboards) ----------------------------------------
    def counter(self, name: str, **labels: str) -> Decimal:
        """Sum of all series of ``name`` whose labels include the given ones."""
        want = set(labels.items())
        return sum((v for k, v in self._counters.get(name, {}).items() if want <= set(k)), Decimal(0))

    def gauge(self, name: str, **labels: str) -> Decimal:
        return self._gauges.get(name, {}).get(_labels(labels), Decimal(0))

    def histogram_count(self, name: str, **labels: str) -> int:
        want = set(labels.items())
        return sum(h.n for k, h in self._hists.get(name, {}).items() if want <= set(k))

    def render(self) -> str:
        """Prometheus text exposition format."""
        out: list[str] = []
        for name in sorted(self._counters):
            out += [f"# HELP {name} {self._help.get(name, name)}", f"# TYPE {name} counter"]
            out += [f"{name}{_fmt(k)} {v}" for k, v in sorted(self._counters[name].items())]
        for name in sorted(self._gauges):
            if not self._gauges[name]:
                continue
            out += [f"# HELP {name} {self._help.get(name, name)}", f"# TYPE {name} gauge"]
            out += [f"{name}{_fmt(k)} {v}" for k, v in sorted(self._gauges[name].items())]
        for name in sorted(self._hists):
            out += [f"# HELP {name} {self._help.get(name, name)}", f"# TYPE {name} histogram"]
            for k, h in sorted(self._hists[name].items()):
                for bound, count in zip(h.buckets, h.counts, strict=True):
                    out.append(f"{name}_bucket{_fmt(k, 'le=' + chr(34) + str(bound) + chr(34))} {count}")
                out.append(f"{name}_bucket{_fmt(k, 'le=' + chr(34) + '+Inf' + chr(34))} {h.n}")
                out.append(f"{name}_sum{_fmt(k)} {h.total}")
                out.append(f"{name}_count{_fmt(k)} {h.n}")
        return "\n".join(out) + ("\n" if out else "")


# Names used across the engine, so dashboards and tests agree on them (CLAUDE.md section 12).
TRADES_STARTED = "oddtrue_trades_started_total"
TRADES_FINISHED = "oddtrue_trades_finished_total"  # label: outcome
TRADES_CANCELLED = "oddtrue_trades_cancelled_total"  # label: reason
REVALIDATION = "oddtrue_revalidation_total"  # label: result=pass|drop  (drop-off rate = drop / (pass + drop))
OPPORTUNITIES_FOUND = "oddtrue_opportunities_found_total"  # fed by detection (Phase 5)
LEG_LATENCY = "oddtrue_leg_latency_seconds"  # label: leg
RECOVERY_COUNT = "oddtrue_recovery_total"
RECOVERY_COST = "oddtrue_recovery_cost_usdt_total"
LOANS_OPEN = "oddtrue_loans_open"
LOAN_OLDEST_AGE = "oddtrue_loan_oldest_open_seconds"
RECONCILIATIONS = "oddtrue_reconciliations_total"  # label: status
RECONCILIATION_MISMATCH = "oddtrue_reconciliation_mismatch_total"
EXCHANGE_CALLS = "oddtrue_exchange_calls_total"  # labels: exchange, result
EXCHANGE_ERRORS = "oddtrue_exchange_errors_total"  # labels: exchange, error
EXCHANGE_LATENCY = "oddtrue_exchange_call_seconds"  # label: exchange
ALERTS = "oddtrue_alerts_total"  # labels: kind, severity
ALERTS_SUPPRESSED = "oddtrue_alerts_suppressed_total"
HALTS_ACTIVE = "oddtrue_halts_active"  # label: scope
CLIENTS_FROZEN = "oddtrue_clients_frozen"
