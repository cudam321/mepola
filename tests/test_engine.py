"""LiveEngine tests — the autonomous lifecycle reproduces the state machine, offline.

Also the Orchestrator's true-candle truth path (reconciliation high-water rule, wick-only
dip entry, restart backfill, anchor fidelity) with a fake datapi charts client."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from memebot.live.engine import LiveEngine
from memebot.live.risk import RiskConfig, RiskGovernor
from memebot.live.run import Orchestrator

def _paper_orch(db):
    """An Orchestrator pinned to PAPER mode — hermetic: these tests exercise the inline paper
    path and must not flip behavior when the operator arms the repo config to mode=live."""
    from memebot.config import Settings
    s = Settings.load()
    s.raw.setdefault("strategy", {}).setdefault("tailrider", {})["mode"] = "paper"
    return Orchestrator(db, settings=s)

from memebot.live.state import LiveState, utcnow
from memebot.live.strategy import PositionState, TailRider
from memebot.models import Candle, Signal, SignalSide

T0 = datetime(2026, 6, 1, 0, 0, 0, tzinfo=timezone.utc)


def _c(i, o, h, l, cl):
    return Candle(ts=T0 + timedelta(minutes=i), open=o, high=h, low=l, close=cl, volume=1.0)


def _sig(mint="MINTpump"):
    return Signal(source_channel="@your_channel", message_id=1, posted_at=T0, raw_text="buy",
                  side=SignalSide.BUY, mint=mint, ticker="AAA", parse_confidence=1.0)


def _engine(tmp_path):
    st = LiveState(tmp_path / "s.db")
    risk = RiskGovernor(st, RiskConfig(stake_usd=3.0))
    return st, LiveEngine(st, risk)


def _candles_dip_then_moon():
    # sig=100 -> dip to 50 (enter 50.5) -> secure at 3x -> ride, then settle
    return [
        _c(0, 100, 100, 100, 100),
        _c(1, 100, 100, 50, 60),                 # ENTER at 50.5
        _c(2, 60, 4 * 50.5, 55, 3.5 * 50.5),     # 3x secure (sell 33%)
        _c(3, 3.5 * 50.5, 7 * 50.5, 3.0 * 50.5, 6.5 * 50.5),   # 6x ride
        _c(4, 6.5 * 50.5, 6.5 * 50.5, 5 * 50.5, 5.5 * 50.5),
    ]


def test_engine_reproduces_standalone_machine(tmp_path):
    st, eng = _engine(tmp_path)
    candles = _candles_dip_then_moon()
    assert eng.ingest_call(_sig(), price=100.0, now=T0)
    for c in candles:
        eng.on_candle("MINTpump", c)
    eng.finalize_token("MINTpump", candles[-1].close, candles[-1].ts)

    # standalone machine on the same candles
    tr = TailRider()
    for c in candles:
        tr.on_candle(c)
    tr.finalize(candles[-1].close, candles[-1].ts)

    closed = st.closed_trades()
    assert len(closed) == 1
    assert closed[0]["realized_multiple"] == pytest.approx(tr.realized_multiple, rel=1e-12)
    assert closed[0]["was_secured"] == 1
    # bankroll reflects the win: pnl = 3*(mult-1)
    assert closed[0]["pnl_usd"] == pytest.approx(3.0 * (tr.realized_multiple - 1.0), rel=1e-12)
    st.close()


def test_engine_dedup_second_call_rejected(tmp_path):
    st, eng = _engine(tmp_path)
    assert eng.ingest_call(_sig(), price=100.0, now=T0) is True
    assert eng.ingest_call(_sig(), price=100.0, now=T0 + timedelta(hours=1)) is False  # dup
    assert len(st.active_positions()) == 1
    st.close()


def test_engine_stop_out_lifecycle(tmp_path):
    st, eng = _engine(tmp_path)
    candles = [
        _c(0, 100, 100, 100, 100),
        _c(1, 100, 100, 50, 60),     # ENTER 50.5
        _c(2, 60, 60, 30, 32),       # low 30 <= 35.35 -> STOP_OUT
    ]
    eng.ingest_call(_sig(), price=100.0, now=T0)
    for c in candles:
        eng.on_candle("MINTpump", c)
    closed = st.closed_trades()
    assert len(closed) == 1 and closed[0]["was_stopped"] == 1
    assert closed[0]["realized_multiple"] == pytest.approx(0.70 * 0.95)
    st.close()


def test_engine_restart_rehydrates_and_resumes(tmp_path):
    st, eng = _engine(tmp_path)
    candles = _candles_dip_then_moon()
    eng.ingest_call(_sig(), price=100.0, now=T0)
    # feed only the first 3 candles (entered + secured), then "crash"
    for c in candles[:3]:
        eng.on_candle("MINTpump", c)
    st.close()

    # restart: new state + engine rebuild from SQLite
    st2 = LiveState(tmp_path / "s.db")
    risk2 = RiskGovernor(st2, RiskConfig(stake_usd=3.0))
    eng2 = LiveEngine(st2, risk2)
    assert "MINTpump" in eng2.riders           # rehydrated
    for c in candles[3:]:
        eng2.on_candle("MINTpump", c)
    eng2.finalize_token("MINTpump", candles[-1].close, candles[-1].ts)

    tr = TailRider()
    for c in candles:
        tr.on_candle(c)
    tr.finalize(candles[-1].close, candles[-1].ts)
    closed = st2.closed_trades()
    assert len(closed) == 1
    assert closed[0]["realized_multiple"] == pytest.approx(tr.realized_multiple, rel=1e-9)
    st2.close()


def test_engine_expires_when_no_dip(tmp_path):
    st, eng = _engine(tmp_path)
    eng.ingest_call(_sig(), price=100.0, now=T0)
    # candles that never dip to 50, spanning > 48h
    for i in range(0, 60):
        eng.on_candle("MINTpump", Candle(ts=T0 + timedelta(hours=i), open=100, high=110,
                                         low=80, close=100, volume=1.0))
    pos = st.get_position("MINTpump")
    assert pos["state"] == "EXPIRED"
    assert len(st.closed_trades()) == 0          # never traded
    st.close()


def test_engine_bankroll_excludes_seed_pnl(tmp_path):
    """REGRESSION (2026-07-03): engine equity must count LIVE trades only (provenance via
    seen_mints.outcome). Seed P&L leaked in and jumped the live equity curve by ~+$394."""
    st, eng = _engine(tmp_path)
    # a SEED trade: outcome 'seen' (the seed script's default), big P&L
    st.mark_seen("SEEDMINT", ticker="S", first_seen_at=T0)                # outcome='seen'
    pid = st.create_position(mint="SEEDMINT", ticker="S", signal_at=T0, signal_price=1.0,
                             state="EXITED")
    st.record_close(position_id=pid, mint="SEEDMINT", ticker="S", entry_at=T0, entry_price=1.0,
                    stake_usd=3.0, exit_at=T0, close_reason="rode_to_horizon",
                    realized_multiple=100.0, pnl_usd=297.0)
    assert eng._realized_equity() == 500.0                                # seed P&L excluded
    # a LIVE trade: engine ingest marks outcome='positioned'
    candles = [
        _c(0, 100, 100, 100, 100),
        _c(1, 100, 100, 50, 60),     # ENTER 50.5
        _c(2, 60, 60, 30, 32),       # STOP_OUT -> realized 0.665x, pnl 3*(0.665-1)
    ]
    eng.ingest_call(_sig("LIVEMINT"), price=100.0, now=T0)
    for c in candles:
        eng.on_candle("LIVEMINT", c)
    live_pnl = 3.0 * (0.70 * 0.95 - 1.0)
    assert eng._realized_equity() == pytest.approx(500.0 + live_pnl)
    # and the sampled bankroll row carries the live-only number
    eng.sample_bankroll(now=candles[-1].ts)
    row = st.bankroll_series()[-1]
    assert row["realized_equity_usd"] == pytest.approx(500.0 + live_pnl)
    st.close()


# --------------------------------------------------------------------------- #
# Orchestrator truth path: true-candle reconciliation / restart backfill / anchor
# fidelity — offline, with a fake datapi charts client.
# --------------------------------------------------------------------------- #

class FakeCharts:
    """Offline stand-in for JupiterChartsClient.fetch_candles (returns candles in range)."""

    def __init__(self, candles=None):
        self.candles = list(candles or [])
        self.calls: list[tuple] = []

    def fetch_candles(self, mint, interval, start, end, *, candles=1000):
        self.calls.append((mint, interval, start, end))
        return sorted((c for c in self.candles if start <= c.ts <= end), key=lambda c: c.ts)


def _mc(ts, o, h, l, cl):
    return Candle(ts=ts, open=o, high=h, low=l, close=cl, volume=1.0)


def test_reconciliation_high_water_rule_and_machine_equivalence(tmp_path):
    """Older candle rejected, newer fed; result == feeding the accepted candles directly."""
    orch = _paper_orch(tmp_path / "s.db")
    now = utcnow().replace(second=0, microsecond=0)
    assert orch.engine.ingest_call(_sig("HWMINT"), price=100.0, now=now)
    c0 = _mc(now, 100, 100, 100, 100)
    c1 = _mc(now + timedelta(minutes=1), 100, 100, 30, 60)          # low 30: would fire a stop
    c2 = _mc(now + timedelta(minutes=2), 60, 4 * 50.5, 48, 3.5 * 50.5)  # dip ENTER + 3x secure

    assert orch._feed_candle("HWMINT", c0) is True
    assert orch._feed_candle("HWMINT", c2) is True                  # newer: fed
    assert orch._feed_candle("HWMINT", c1) is False                 # OLDER: rejected (stale)
    assert orch._feed_candle("HWMINT", c2) is False                 # equal ts: rejected

    tr = TailRider()                                                # same candles, directly
    for c in (c0, c2):
        tr.on_candle(c)
    live = orch.engine.riders["HWMINT"]
    assert live.state is tr.state and live.state is PositionState.SECURED
    assert live.pr == pytest.approx(tr.pr, rel=1e-12)
    assert live.rem == pytest.approx(tr.rem, rel=1e-12)
    orch.state.close()


def test_wick_only_dip_enters_via_reconciliation(tmp_path):
    """Spot ticks never cross the -50% trigger, but the TRUE 1m candle's low does -> ENTER."""
    orch = _paper_orch(tmp_path / "s.db")
    now = utcnow()
    assert orch.engine.ingest_call(_sig("WICKMINT"), price=100.0, now=now - timedelta(minutes=5))
    orch._anchored.add("WICKMINT")          # anchor pass is under test elsewhere
    for i, px in enumerate((100.0, 80.0, 60.0, 55.0)):              # never <= 50
        orch._on_tick("WICKMINT", px, now - timedelta(seconds=90 - i))
    assert orch.engine.riders["WICKMINT"].state is PositionState.WATCHING

    orch.charts = FakeCharts([_mc(now - timedelta(seconds=60), 60, 60, 45, 58)])  # wick to 45
    asyncio.run(orch._reconcile_mint("WICKMINT"))
    tr = orch.engine.riders["WICKMINT"]
    assert tr.state is PositionState.ENTERED
    assert tr.entry == pytest.approx(50.0 * 1.01)                   # filled AT the level
    orch.state.close()


def test_restart_backfill_catches_downtime_dip(tmp_path):
    """A dip candle that printed during a deploy gap is replayed through engine AND shadow."""
    db = tmp_path / "s.db"
    orch1 = _paper_orch(db)
    now = utcnow()
    assert orch1.engine.ingest_call(_sig("GAPMINT"), price=100.0, now=now - timedelta(minutes=10))
    orch1.shadow.ingest("GAPMINT", 100.0, (now - timedelta(minutes=10)).timestamp(), ticker="AAA")
    orch1.state.close()                     # "deploy": the process dies; a wick prints

    orch2 = _paper_orch(db)
    assert "GAPMINT" in orch2.engine.riders                          # rehydrated WATCHING
    assert "GAPMINT" in orch2._backfill_targets                      # scheduled for backfill
    orch2.charts = FakeCharts([_mc(now - timedelta(seconds=60), 90, 90, 42, 70)])
    asyncio.run(orch2._backfill())
    tr = orch2.engine.riders["GAPMINT"]
    assert tr.state is PositionState.ENTERED                         # downtime wick recovered
    assert tr.entry == pytest.approx(50.0 * 1.01)
    # the shadow challengers saw the same candle (C1 mirrors the champion by construction)
    assert orch2.shadow.riders["GAPMINT"]["C1"].cur.state is PositionState.ENTERED
    assert orch2._backfill_targets == {}                             # backfill runs exactly once
    orch2.state.close()


def test_reanchor_updates_sig_once_and_never_after_entry(tmp_path):
    orch = _paper_orch(tmp_path / "s.db")
    now = utcnow()
    sig_at = now - timedelta(minutes=2)
    spot, first_open = 1.527e-4, 1.889e-4                            # the $DIP divergence
    assert orch.engine.ingest_call(_sig("ANCMINT"), price=spot, now=sig_at)
    orch.shadow.ingest("ANCMINT", spot, sig_at.timestamp(), ticker="AAA")
    minute0 = sig_at.replace(second=0, microsecond=0)
    orch.charts = FakeCharts([_mc(minute0 + timedelta(minutes=1),
                                  first_open, 2.0e-4, 1.8e-4, 1.9e-4)])

    asyncio.run(orch._maybe_reanchor("ANCMINT"))
    assert orch.engine.riders["ANCMINT"].sig == pytest.approx(first_open)
    assert orch.state.get_position("ANCMINT")["signal_price"] == pytest.approx(first_open)
    r = orch.shadow.riders["ANCMINT"]["C1"]
    assert r.sig == pytest.approx(first_open)
    assert r.cur.sig == pytest.approx(first_open)
    assert "ANCMINT" in orch._anchored

    # exactly once: a later (different) first candle must NOT re-anchor again
    orch.charts = FakeCharts([_mc(minute0 + timedelta(minutes=1), 9.9e-4, 9.9e-4, 9.9e-4, 9.9e-4)])
    asyncio.run(orch._maybe_reanchor("ANCMINT"))
    assert orch.engine.riders["ANCMINT"].sig == pytest.approx(first_open)

    # never after entry: an ENTERED rider keeps its anchor untouched
    assert orch.engine.ingest_call(_sig("ENTMINT"), price=100.0, now=sig_at)
    orch._on_tick("ENTMINT", 49.0, now)                              # dip tick -> ENTER
    assert orch.engine.riders["ENTMINT"].state is PositionState.ENTERED
    orch.charts = FakeCharts([_mc(minute0 + timedelta(minutes=1), 200.0, 200.0, 200.0, 200.0)])
    asyncio.run(orch._maybe_reanchor("ENTMINT"))
    assert orch.engine.riders["ENTMINT"].sig == 100.0
    assert orch.state.get_position("ENTMINT")["signal_price"] == 100.0
    orch.state.close()


def test_reanchor_resets_low_watermark(tmp_path):
    """REGRESSION (2026-07-03): a WATCHING row showed "low 100%" — impossible without an
    entry. The low_price watermark recorded under the OLD spot anchor is meaningless
    against the re-anchored sig; reanchor must reset it in the rider AND the DB row."""
    st, eng = _engine(tmp_path)
    eng.ingest_call(_sig("WMMINT"), price=100.0, now=T0)
    eng.on_candle("WMMINT", _c(1, 100, 100, 80, 90))       # watermark low=80; no dip trigger
    tr = eng.riders["WMMINT"]
    assert tr.low_price == 80
    assert st.get_position("WMMINT")["low_price"] == 80
    assert eng.reanchor("WMMINT", 120.0) is True
    assert tr.low_price is None
    assert st.get_position("WMMINT")["low_price"] is None  # NULL in positions too
    assert st.get_position("WMMINT")["signal_price"] == 120.0
    st.close()


def test_pre_signal_candles_are_never_processed(tmp_path):
    """REGRESSION (2026-07-03): an unclamped backfill fed candles from BEFORE the signal
    and manufactured an entry dated weeks before the call. The engine must drop any candle
    older than the rider's t0 (the ingest moment) — exactly the backtest's ts >= posted_at."""
    st, eng = _engine(tmp_path)
    eng.ingest_call(_sig(), price=100.0, now=T0)
    # pre-signal candle with a low deep below the trigger — must be IGNORED
    pre = Candle(ts=T0 - timedelta(days=30), open=100, high=100, low=1.0, close=2.0, volume=1.0)
    eng.on_candle("MINTpump", pre)
    pos = st.get_position("MINTpump")
    assert pos["state"] == "WATCHING" and pos["entry_price"] is None
    # a post-signal dip still enters normally
    eng.on_candle("MINTpump", _c(1, 100, 100, 49, 60))
    assert st.get_position("MINTpump")["state"] == "ENTERED"
    st.close()


def test_repair_presignal_trades_resets_to_watching(tmp_path):
    """The trade_fix_v3 migration unwinds fictional pre-signal trades."""
    from memebot.live.run import repair_presignal_trades

    st, eng = _engine(tmp_path)
    eng.ingest_call(_sig(), price=100.0, now=T0)
    # manufacture the damage: an ENTER event BEFORE signal_at + a closed trade
    pid = st.get_position("MINTpump")["id"]
    st.append_event(position_id=pid, mint="MINTpump", ts=T0 - timedelta(days=10),
                    event_type="ENTER", price=50.5, note="fictional pre-signal entry")
    st.update_position("MINTpump", state="STOPPED", entry_price=50.5, stake_usd=3.0,
                       realized_multiple=0.665, realized_pnl_usd=-1.005)
    st.record_close(position_id=pid, mint="MINTpump", ticker="AAA",
                    entry_at=T0 - timedelta(days=10), entry_price=50.5, stake_usd=3.0,
                    exit_at=T0 - timedelta(days=10), close_reason="stopped",
                    realized_multiple=0.665, pnl_usd=-1.005, was_stopped=True)

    n = repair_presignal_trades(st)
    assert n == 1
    pos = st.get_position("MINTpump")
    assert pos["state"] == "WATCHING" and pos["entry_price"] is None
    assert pos["realized_multiple"] is None and pos["realized_pnl_usd"] == 0
    assert st.closed_trades() == []
    events = st.events_for("MINTpump")
    assert [e["event_type"] for e in events] == ["SIGNAL"]
    assert repair_presignal_trades(st) == 0     # idempotent
    st.close()


def test_rejected_call_is_not_blacklisted_and_can_re_enter(tmp_path):
    """F27: a mint declined by a LIVE cap (here the kill-switch) must NOT be marked seen —
    else the is_seen guard blacklists it forever and a later re-call can never enter."""
    st, eng = _engine(tmp_path)
    st.set_system("kill_switch", "on")             # force a capacity-style rejection
    assert eng.ingest_call(_sig("REJMINT"), price=100.0, now=T0) is False
    assert st.is_seen("REJMINT") is False          # NOT blacklisted
    assert st.get_position("REJMINT") is None       # no position created
    sigs = st.query("SELECT accepted, reject_reason FROM signals WHERE mint='REJMINT'")
    assert sigs and sigs[0]["accepted"] == 0 and sigs[0]["reject_reason"] == "kill"  # audit trail kept
    # kill cleared -> a later re-call of the SAME mint now enters
    st.set_system("kill_switch", "off")
    assert eng.ingest_call(_sig("REJMINT"), price=100.0, now=T0 + timedelta(minutes=5)) is True
    assert st.is_seen("REJMINT") is True
    assert st.get_position("REJMINT")["state"] == "WATCHING"
    st.close()


def test_supervise_survives_a_task_crash_and_alerts(tmp_path):
    """F05: one loop crashing must NOT propagate (the top-level gather used to kill the whole
    process); the supervisor catches it, records a CRIT alert, and keeps the task alive."""
    orch = _paper_orch(tmp_path / "s.db")
    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")          # first run crashes
        await asyncio.sleep(3600)               # restart blocks until cancelled

    async def drive():
        task = asyncio.create_task(orch._supervise("t", flaky))
        await asyncio.sleep(0.05)               # let it crash + record the alert
        assert calls["n"] == 1                  # crashed once, did NOT propagate out
        assert not task.done()                  # supervisor survived the crash
        kinds = [a["kind"] for a in orch.state.recent_alerts()]
        assert any(k.startswith("TASK_CRASH") for k in kinds)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(drive())
    orch.state.close()
