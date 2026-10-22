"""Structured logs, secret redaction, metrics and the alert hub (CLAUDE.md rule 9, section 12)."""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator
from decimal import Decimal as D

import pytest

from oddtrue.exchanges.fake import Fault, FaultKind, Op
from oddtrue.ledger.models import Outcome
from oddtrue.observability import metrics as m
from oddtrue.observability.alerts import Alert, AlertHub, AlertKind, ListAlertSink
from oddtrue.observability.logging import (
    REDACTED,
    configure_logging,
    current_context,
    forget_secrets,
    get_logger,
    log_context,
    redact,
    redact_text,
    register_secret,
)
from oddtrue.observability.metrics import Metrics
from tests.world import Safe, World

FAKE_KEY = "AKIAFAKEFAKEFAKE12345"  # obviously fake: only used to prove it never reaches a log line


@pytest.fixture
def logbuf() -> Iterator[io.StringIO]:
    buf = io.StringIO()
    handler = configure_logging(logging.DEBUG, buf)
    yield buf
    logging.getLogger("oddtrue").removeHandler(handler)
    forget_secrets()


def lines(buf: io.StringIO) -> list[dict[str, object]]:
    return [json.loads(x) for x in buf.getvalue().splitlines() if x]


# ---- logs ----------------------------------------


def test_every_line_is_json_with_the_bound_trade_context(logbuf: io.StringIO) -> None:
    log = get_logger("test")
    with log_context(trade_id="t1", client_id="john", exchange="bitget"):
        with log_context(leg="B_SELL", state="EXECUTING", client_order_id="ot-abc", unused=None):
            log.info("sold", extra={"filled_qty": "0.004"})
            assert current_context()["leg"] == "B_SELL" and "unused" not in current_context()
        log.info("back to the outer scope")
    log.info("no context at all")
    first, second, third = lines(logbuf)
    for key, value in {
        "trade_id": "t1", "client_id": "john", "exchange": "bitget", "leg": "B_SELL",
        "state": "EXECUTING", "client_order_id": "ot-abc", "filled_qty": "0.004", "msg": "sold", "level": "INFO",
    }.items():  # fmt: skip
        assert first[key] == value
    assert "leg" not in second and second["trade_id"] == "t1"
    assert "trade_id" not in third and third["logger"] == "oddtrue.test" and "ts" in third


@pytest.mark.parametrize(
    "key",
    ["api_key", "API-KEY", "apiSecret", "secret", "Passphrase", "password", "access_token", "Authorization",
     "signature", "X-MBX-APIKEY", "OK-ACCESS-SIGN", "OK-ACCESS-PASSPHRASE", "private_key", "otp", "mfa_code",
     "dek_ref"],
)  # fmt: skip
def test_secret_looking_fields_never_keep_their_value(key: str) -> None:
    assert redact({key: "hunter2hunter2", "client_order_id": "ot-abc", "qty": D("0.004")}) == {
        key: REDACTED, "client_order_id": "ot-abc", "qty": D("0.004")
    }  # fmt: skip


def test_redaction_walks_nested_containers_and_free_text() -> None:
    payload = {"request": {"headers": {"X-MBX-APIKEY": FAKE_KEY}, "args": [{"secret": "s3cr3t-value"}, "ok"]}}
    assert redact(payload) == {"request": {"headers": {"X-MBX-APIKEY": REDACTED}, "args": [{"secret": REDACTED}, "ok"]}}
    for text in (
        f"GET /api?symbol=BTCUSDT&signature=abcdef0123456789&timestamp=1 key={FAKE_KEY}",
        f'{{"api_key": "{FAKE_KEY}", "side": "buy"}}',
        f"passphrase: {FAKE_KEY}",
        f"Authorization: Bearer {FAKE_KEY}.payload",
        f"token={FAKE_KEY}",
    ):
        cleaned = redact_text(text)
        assert FAKE_KEY not in cleaned and "abcdef0123456789" not in cleaned and REDACTED in cleaned, cleaned
    assert redact_text("order ot-abc123 filled 0.004 BTC at 100900") == "order ot-abc123 filled 0.004 BTC at 100900"
    assert redact_text("cache key=BTCUSDT hit") == "cache key=BTCUSDT hit"  # a short, ordinary 'key' is left alone
    assert redact(5) == 5 and redact(None) is None


def test_a_registered_secret_is_masked_wherever_it_appears() -> None:
    register_secret("decrypted-key-material-9f8e")
    register_secret("abc")  # too short to be a safe thing to blanket-replace: ignored
    try:
        out = redact_text("loaded decrypted-key-material-9f8e for client; abc stays")
        assert out == f"loaded {REDACTED} for client; abc stays"
    finally:
        forget_secrets()
    assert redact_text("decrypted-key-material-9f8e") == "decrypted-key-material-9f8e"


def test_secrets_are_scrubbed_from_tracebacks_and_messages(logbuf: io.StringIO) -> None:
    register_secret(FAKE_KEY)
    log = get_logger("test")
    try:
        raise RuntimeError(f"request failed: api_key={FAKE_KEY} signature=deadbeef12345678")
    except RuntimeError:
        log.exception("call to exchange %s failed", FAKE_KEY)
    raw = logbuf.getvalue()
    assert FAKE_KEY not in raw and "deadbeef12345678" not in raw
    record = lines(logbuf)[0]
    assert record["level"] == "ERROR" and "Traceback" in str(record["exc"]) and REDACTED in str(record["exc"])


async def test_a_real_trade_logs_its_lifecycle_with_context_and_no_secret(world: World, logbuf: io.StringIO) -> None:
    register_secret("SUPERSECRETVALUE123")
    world.b.add_fault(Op.PLACE_ORDER, Fault(FaultKind.REJECT, code="secret=SUPERSECRETVALUE123"))
    result = await world.engine().start(world.plan("t1"))
    assert result.outcome is Outcome.RECOVERED
    raw = logbuf.getvalue()
    assert "SUPERSECRETVALUE123" not in raw  # the exchange's error text carried it; the log did not
    records = lines(logbuf)
    assert records and all(r["client_id"] == "john" and r["trade_id"] == "t1" for r in records)
    moves = [r["msg"] for r in records if "->" in str(r["msg"])]
    assert "ALLOCATED -> PRECHECK_B_LIQUIDITY: allocated" in moves and any("RECOVERY" in str(x) for x in moves)
    sell = next(r for r in records if r["msg"] == "leg B_SELL REJECTED")
    assert sell["leg"] == "B_SELL" and sell["exchange"] == "bitget" and str(sell["client_order_id"]).startswith("ot-")
    assert next(r for r in records if "trade finished" in str(r["msg"]))["msg"] == "trade finished: RECOVERED"


# ---- metrics ----------------------------------------


def test_counters_gauges_and_histograms() -> None:
    reg = Metrics()
    reg.describe("jobs_total", "jobs run")
    reg.inc("jobs_total", outcome="ok")
    reg.inc("jobs_total", 2, outcome="ok")
    reg.inc("jobs_total", D("0.5"), outcome="bad")
    assert reg.counter("jobs_total", outcome="ok") == 3 and reg.counter("jobs_total") == D("3.5")
    assert reg.counter("nothing") == 0
    with pytest.raises(ValueError, match="only go up"):
        reg.inc("jobs_total", -1)
    reg.set("queue", 7)
    reg.set("queue", 4)
    assert reg.gauge("queue") == 4 and reg.gauge("missing") == 0
    reg.clear_gauge("queue")
    assert reg.gauge("queue") == 0
    reg.observe("lat", 0.03, leg="A_BUY")
    reg.observe("lat", 3.0, leg="A_BUY")
    assert reg.histogram_count("lat") == 2 and reg.histogram_count("lat", leg="B_SELL") == 0


def test_prometheus_text_exposition() -> None:
    reg = Metrics()
    reg.describe("jobs_total", "jobs run")
    reg.inc("jobs_total", outcome="ok")
    reg.set("depth", 3, queue="a")
    reg.set("empty", 1)
    reg.clear_gauge("empty")
    reg.observe("lat", 0.2, buckets=(0.1, 0.5), leg="X")
    text = reg.render()
    assert '# HELP jobs_total jobs run\n# TYPE jobs_total counter\njobs_total{outcome="ok"} 1' in text
    assert '# TYPE depth gauge\ndepth{queue="a"} 3' in text and "empty" not in text
    assert 'lat_bucket{leg="X",le="0.1"} 0' in text and 'lat_bucket{leg="X",le="0.5"} 1' in text
    assert (
        'lat_bucket{leg="X",le="+Inf"} 1' in text and 'lat_sum{leg="X"} 0.2' in text and 'lat_count{leg="X"} 1' in text
    )
    assert Metrics().render() == ""


async def test_the_metrics_section_12_asks_for_are_recorded_by_real_trades(safe: Safe) -> None:
    world, mt = safe.world, safe.metrics
    assert (await safe.engine().start(world.plan("t1"))).outcome is Outcome.SUCCESS
    world.b.add_fault(Op.PLACE_ORDER, Fault(FaultKind.REJECT))
    assert (await safe.engine().start(world.plan("t2"))).outcome is Outcome.RECOVERED
    world.set_books(bid_b="100050")
    assert (await safe.engine().start(world.plan("t3"))).outcome is Outcome.CANCELLED
    assert mt.counter(m.TRADES_STARTED) == 3
    assert (
        mt.counter(m.TRADES_FINISHED, outcome="SUCCESS") == 1
        and mt.counter(m.TRADES_FINISHED, outcome="RECOVERED") == 1
    )
    assert mt.counter(m.TRADES_CANCELLED, reason="validate_trade_failed") == 1
    assert mt.counter(m.REVALIDATION, result="pass") == 2 and mt.counter(m.REVALIDATION, result="drop") == 1  # drop-off
    assert mt.counter(m.RECOVERY_COUNT) == 1
    cost = -(await world.ledger.trade_result("john", "t2")).net
    assert cost > 0 and mt.counter(m.RECOVERY_COST) == cost  # the real cost, from actual fills
    for leg in ("A_BUY", "B_BORROW", "B_SELL", "B_REPAY"):
        assert mt.histogram_count(m.LEG_LATENCY, leg=leg) >= 1, leg
    text = mt.render()
    assert "oddtrue_trades_finished_total" in text and "oddtrue_leg_latency_seconds_bucket" in text


# ---- alert hub ----------------------------------------


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


class _Channel:
    def __init__(self, fail: bool = False) -> None:
        self.paged: list[Alert] = []
        self.fail = fail

    async def notify(self, alert: Alert) -> None:
        if self.fail:
            raise ConnectionError("pager down")
        self.paged.append(alert)


async def test_identical_alerts_are_deduplicated_inside_the_window_and_counted(safe: Safe) -> None:
    clock, mt = _Clock(), Metrics()
    hub = AlertHub(clock, mt, store=safe.safety, dedupe_s=60)
    a = Alert(AlertKind.NAKED_POSITION, "john", "t1", "short 0.004 BTC")
    hub.raise_alert(a)
    hub.raise_alert(a)
    clock.now += 61
    hub.raise_alert(a)
    hub.raise_alert(Alert(AlertKind.NAKED_POSITION, "john", "t1", "a different detail"))
    assert len(hub.alerts) == 3 and hub.kinds().count(AlertKind.NAKED_POSITION) == 3
    assert mt.counter(m.ALERTS, kind="naked_position", severity="PAGE") == 3 and mt.counter(m.ALERTS_SUPPRESSED) == 1


async def test_alerts_are_persisted_redacted_and_only_page_alerts_reach_channels(safe: Safe) -> None:
    channel = _Channel()
    hub = AlertHub(_Clock(), Metrics(), store=safe.safety, channels=(channel,))
    hub.raise_alert(Alert(AlertKind.AUTH_INCIDENT, "john", "t1", f"bad key api_key={FAKE_KEY}"))
    hub.raise_alert(Alert(AlertKind.DUST_LEFT, "john", "t1", "0.000006 BTC left", page=False))
    await hub.flush()
    await hub.flush()  # nothing pending: a no-op
    rows = await safe.safety.alerts(client_id="john")
    assert [(r.severity, r.kind) for r in rows] == [("PAGE", "auth_incident"), ("WARN", "dust_left")]
    assert FAKE_KEY not in rows[0].detail and REDACTED in rows[0].detail
    assert [a.kind for a in channel.paged] == [AlertKind.AUTH_INCIDENT]
    assert await safe.safety.acknowledge_alert(rows[0].id, "ops:alice") is True
    assert await safe.safety.acknowledge_alert(rows[0].id, "ops:bob") is False  # already acknowledged
    assert (await safe.safety.alerts(kind="auth_incident"))[0].acknowledged_by == "ops:alice"


async def test_a_broken_pager_or_database_never_breaks_trading() -> None:
    class BrokenStore:
        async def save_alerts(self, rows: object) -> int:
            raise ConnectionError("db down")

    mt, good = Metrics(), _Channel()
    hub = AlertHub(_Clock(), mt, store=BrokenStore(), channels=(_Channel(fail=True), good))  # type: ignore[arg-type]
    hub.raise_alert(Alert(AlertKind.NAKED_POSITION, "john", "t1", "short"))
    await hub.flush()  # raises nothing
    assert [a.kind for a in good.paged] == [AlertKind.NAKED_POSITION]  # later channels still get it
    assert (
        mt.counter(m.ALERTS_SUPPRESSED, kind="persist_failed") == 1
        and mt.counter(m.ALERTS_SUPPRESSED, kind="channel_failed") == 1
    )


async def test_the_in_memory_sink_collects_alerts() -> None:
    sink = ListAlertSink()
    sink.raise_alert(Alert(AlertKind.DUST_LEFT, "john", "t1", "x", page=False))
    await sink.flush()
    assert sink.kinds() == [AlertKind.DUST_LEFT]
