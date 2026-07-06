"""Live execution pipeline + engine live-path tests (the F26 / advance-after-confirm refactor).

All offline with a fake executor — the path stays INERT; these prove it is CORRECT when armed:
advance-after-confirm, real-fill accounting (F03), rollback-on-failure + retry, busy-rider buffering,
not-armed inertness, multi-event candles, and the off-loop worker plumbing.
See docs/LIVE_EXECUTION_PIPELINE.md."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from memebot.live.engine import LiveEngine
from memebot.live.execution import ExecJob, LiveExecutionPipeline
from memebot.live.executor import Fill, SwapNotConfirmed
from memebot.live.risk import RiskConfig, RiskGovernor
from memebot.live.state import LiveState
from memebot.live.strategy import Event, PositionState, TailRiderConfig
from memebot.models import Candle, Signal, SignalSide

T0 = datetime(2026, 6, 1, tzinfo=timezone.utc)


def _c(i, o, h, l, cl):
    return Candle(ts=T0 + timedelta(minutes=i), open=o, high=h, low=l, close=cl, volume=1.0)


class FakeExec:
    """Stand-in LiveExecutor: records calls, returns Fills, or raises SwapNotConfirmed."""
    mode = "live"

    def __init__(self):
        self.armed = True
        self.dry_run = True                  # -> _can_send_live skips the equivalence/dust gates
        self.fail = False
        self.fail_sells = False
        self.buy_tokens = 2.0
        self.sell_usd = 5.0
        self.buys: list[str] = []
        self.sells: list[tuple[str, str]] = []

    def buy(self, *, mint, stake_usd, entry_price, ts=None):
        if self.fail:
            raise SwapNotConfirmed("not confirmed")
        self.buys.append(mint)
        return Fill(mint, "ENTRY", entry_price, self.buy_tokens, stake_usd, ts=ts, note="fake buy")

    def sell_event(self, *, mint, stake_usd, entry_price, event, tokens_qty=None,
                   target_remaining_tokens=None):
        if self.fail or self.fail_sells:
            raise SwapNotConfirmed("not confirmed")
        self.sells.append((mint, event.kind))
        return Fill(mint, "SELL", event.price, 0.0, self.sell_usd, ts=event.ts, note="fake sell")


def _live_engine(tmp_path):
    st = LiveState(tmp_path / "s.db")
    st.set_system("mode", "live")
    st.set_system("kill_switch", "off")
    fe = FakeExec()
    pipe = LiveExecutionPipeline(fe, on_result=lambda r: None)
    eng = LiveEngine(st, RiskGovernor(st, RiskConfig()), executor=fe, cfg=TailRiderConfig(), pipeline=pipe)
    pipe.on_result = eng.apply_fill_result
    return st, eng, fe, pipe


def _ingest(st, eng, mint, price):
    sig = Signal(source_channel="@c", message_id=1, posted_at=T0, raw_text="buy",
                 side=SignalSide.BUY, mint=mint, ticker="A", parse_confidence=1.0)
    assert eng.ingest_call(sig, price=price, now=T0)


def _drain(pipe, eng):
    """Process every queued job (simulating the worker + the loop apply), including any jobs a
    re-fed buffered candle submits, until the queue is empty."""
    while not pipe._q.empty():
        job = pipe._q.get_nowait()
        eng.apply_fill_result(pipe.execute(job))


# -- advance-after-confirm ---------------------------------------------------- #
def test_enter_advances_only_after_confirm_with_real_tokens(tmp_path):
    st, eng, fe, pipe = _live_engine(tmp_path)
    fe.buy_tokens = 7.5
    _ingest(st, eng, "M", 100.0)
    eng.on_candle("M", _c(2, 100, 100, 49, 60))          # dip -> ENTER intent submitted
    assert "M" in eng._pending
    assert st.get_position("M")["state"] == "WATCHING"   # NOT advanced yet (advance-after-confirm)
    assert pipe._q.qsize() == 1
    _drain(pipe, eng)
    assert "M" not in eng._pending
    pos = st.get_position("M")
    assert pos["state"] == "ENTERED"
    assert pos["tokens_qty"] == 7.5                      # REAL tokens from the fill, not the quote
    assert fe.buys == ["M"]
    st.close()


# -- rollback on failure, then retry ------------------------------------------ #
def test_failed_buy_rolls_back_no_phantom_then_retries(tmp_path):
    st, eng, fe, pipe = _live_engine(tmp_path)
    fe.fail = True
    _ingest(st, eng, "M", 100.0)
    eng.on_candle("M", _c(2, 100, 100, 49, 60))
    _drain(pipe, eng)
    assert "M" not in eng._pending
    assert st.get_position("M")["state"] == "WATCHING"   # rolled back — NO phantom position
    assert eng.riders["M"].state is PositionState.WATCHING
    fe.fail = False                                       # the fault clears
    eng.on_candle("M", _c(3, 100, 100, 49, 60))          # next candle retries
    _drain(pipe, eng)
    assert st.get_position("M")["state"] == "ENTERED"
    st.close()


# -- busy rider buffers candles, re-feeds after resolve ----------------------- #
def test_busy_rider_buffers_then_refeeds(tmp_path):
    st, eng, fe, pipe = _live_engine(tmp_path)
    _ingest(st, eng, "M", 100.0)
    eng.on_candle("M", _c(2, 100, 100, 49, 60))          # ENTER pending
    assert "M" in eng._pending
    eng.on_candle("M", _c(3, 60, 3 * 51, 55, 2 * 51))    # would secure — but pending -> buffered
    assert eng._buffered.get("M") is not None
    assert fe.sells == []                                # nothing sold while pending
    _drain(pipe, eng)                                    # applies ENTER, re-feeds buffer -> secure job -> applies
    assert any(k == "TP" for _, k in fe.sells)           # the buffered secure eventually executed
    assert st.get_position("M")["state"] in ("SECURED", "RIDING")
    st.close()


# -- inertness: not armed / gated never submits ------------------------------- #
def test_not_armed_never_submits_stays_watching(tmp_path):
    st, eng, fe, pipe = _live_engine(tmp_path)
    fe.armed = False
    _ingest(st, eng, "M", 100.0)
    eng.on_candle("M", _c(2, 100, 100, 49, 60))
    assert "M" not in eng._pending and pipe._q.empty()   # nothing submitted
    assert st.get_position("M")["state"] == "WATCHING"
    st.close()


def test_real_send_gates_block_submit_until_set(tmp_path):
    st, eng, fe, pipe = _live_engine(tmp_path)
    fe.dry_run = False                                   # real-send mode needs the operator gates
    _ingest(st, eng, "M", 100.0)
    eng.on_candle("M", _c(2, 100, 100, 49, 60))
    assert pipe._q.empty()                               # gates unset -> no submit
    st.set_system("equivalence_ok", "1")
    st.set_system("dust_reconciled", "1")
    eng.on_candle("M", _c(3, 100, 100, 49, 60))
    assert pipe._q.qsize() == 1                          # gates set -> submits
    st.close()


# -- real P&L accounting (F03) ------------------------------------------------ #
def test_live_close_books_real_pnl_not_modeled(tmp_path):
    st, eng, fe, pipe = _live_engine(tmp_path)
    fe.buy_tokens = 1.0
    _ingest(st, eng, "M", 100.0)
    eng.on_candle("M", _c(2, 100, 100, 49, 60)); _drain(pipe, eng)     # ENTER (stake $3)
    fe.sell_usd = 9.0
    eng.finalize_token("M", 60.0, ts=T0 + timedelta(hours=1)); _drain(pipe, eng)
    ct = st.closed_trades()
    assert len(ct) == 1
    assert ct[0]["pnl_usd"] == pytest.approx(6.0)         # REAL: proceeds 9 − real cost 3
    assert ct[0]["realized_multiple"] == pytest.approx(3.0)   # REAL: 9 / 3
    st.close()


# -- multi-event candle: swaps placed in order -------------------------------- #
def test_multi_event_candle_executes_enter_then_tp(tmp_path):
    st, eng, fe, pipe = _live_engine(tmp_path)
    _ingest(st, eng, "M", 100.0)
    eng.on_candle("M", _c(2, 100, 3 * 51, 49, 2 * 51))   # dips to 49 AND high hits 3x secure
    _drain(pipe, eng)
    assert fe.buys == ["M"]
    assert any(k == "TP" for _, k in fe.sells)
    assert st.get_position("M")["state"] in ("SECURED", "RIDING")
    st.close()


# -- partial multi-leg: a confirmed ENTER is NOT discarded when the TP fails (F1) ---- #
def test_partial_multileg_commits_enter_when_tp_fails(tmp_path):
    st, eng, fe, pipe = _live_engine(tmp_path)
    fe.buy_tokens = 4.0
    fe.fail_sells = True                                  # the ENTER confirms; the re-fed TP fails
    _ingest(st, eng, "M", 100.0)
    eng.on_candle("M", _c(2, 100, 3 * 51, 49, 2 * 51))   # dip to 49 AND high hits 3x
    _drain(pipe, eng)
    pos = st.get_position("M")
    assert pos["state"] == "ENTERED"                     # ENTER committed, NOT rolled back to WATCHING
    assert pos["tokens_qty"] == 4.0                      # real bought bag persisted
    assert fe.buys == ["M"] and fe.sells == []           # bought; the failed TP was not booked
    assert eng.riders["M"].state is PositionState.ENTERED  # rider rolled back only to post-ENTER
    st.close()


# -- restart reconciliation: a crash mid-submit adopts the landed buy (F2) ---------- #
def _live_orchestrator(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMEBOT_LIVE_ARMED", "1")
    monkeypatch.setenv("MEMEBOT_LIVE_SEND", "1")
    # hermeticity: a live Orchestrator now spins up the paper twin — pin its DB to tmp so the suite
    # never writes/mutates the repo's runs/paper_state.db (audit reverify-3 C4, proven).
    monkeypatch.setenv("MEMEBOT_PAPER_DB", str(tmp_path / "paper.db"))
    from memebot.config import Settings
    from memebot.live.run import Orchestrator
    s = Settings.load()
    s.raw.setdefault("strategy", {}).setdefault("tailrider", {})["mode"] = "live"
    orch = Orchestrator(tmp_path / "s.db", settings=s)
    assert orch.pipeline is not None and not orch.executor.dry_run
    return orch


def _submitted_watcher(orch, mint):
    st = orch.state
    pid = st.create_position(mint=mint, ticker=mint, signal_at=T0, signal_price=100.0,
                             state="WATCHING", t0_epoch=T0.timestamp())
    st.append_event(position_id=pid, mint=mint, ts=T0, event_type="ENTER_SUBMITTED",
                    price=50.5, remaining_frac=1.0)     # crash left this intent unresolved
    return pid


def test_restart_adopts_landed_buy(tmp_path, monkeypatch):
    orch = _live_orchestrator(tmp_path, monkeypatch)
    _submitted_watcher(orch, "R")
    monkeypatch.setattr(orch, "_held_tokens", lambda mint: 5.0)   # the buy actually landed on-chain
    asyncio.run(orch._reconcile_submitted_intents())
    pos = orch.state.get_position("R")
    assert pos["state"] == "ENTERED" and pos["tokens_qty"] == 5.0   # adopted — no re-buy
    assert pos["entry_price"] == 50.5 and pos["stop_price"] == pytest.approx(0.70 * 50.5)
    assert orch.engine.riders["R"].state.value == "ENTERED"
    orch.state.close()


def test_restart_leaves_unlanded_buy_watching(tmp_path, monkeypatch):
    orch = _live_orchestrator(tmp_path, monkeypatch)
    _submitted_watcher(orch, "N")
    monkeypatch.setattr(orch, "_held_tokens", lambda mint: 0.0)   # the buy did NOT land
    asyncio.run(orch._reconcile_submitted_intents())
    assert orch.state.get_position("N")["state"] == "WATCHING"    # stays watching -> dip window retries
    orch.state.close()


# -- the off-loop worker plumbing (real thread + loop delivery) --------------- #
def test_pipeline_worker_delivers_on_loop_without_blocking():
    got = []
    pipe = LiveExecutionPipeline(FakeExec(), on_result=got.append)

    async def drive():
        pipe.start(asyncio.get_running_loop())
        job = ExecJob("M", 1, 3.0, 1.0,
                      [Event(ts=T0, kind="ENTER", price=1.0, frac=0.0, remaining_frac=1.0)], T0)
        pipe.submit(job)                                 # returns immediately (never blocks the loop)
        for _ in range(100):
            if got:
                break
            await asyncio.sleep(0.02)
        pipe.stop()

    asyncio.run(drive())
    assert len(got) == 1 and got[0].ok and got[0].mint == "M"


def test_workers_run_disjoint_mints_concurrently():
    """N swaps on DIFFERENT mints run in parallel (a mass stop-out doesn't serialize). With a
    per-swap latency L and a worker pool, 6 jobs finish in ~L, not 6·L."""
    import time
    LAT = 0.3

    class SlowExec:
        armed = True
        dry_run = True

        def buy(self, *, mint, stake_usd, entry_price, ts=None):
            time.sleep(LAT)                          # simulate the ~30s send+confirm (off-loop)
            return Fill(mint, "ENTRY", entry_price, 1.0, stake_usd, ts=ts, note="slow")

        def sell_event(self, *a, **k):
            raise AssertionError("no sells in this test")

    got = []
    pipe = LiveExecutionPipeline(SlowExec(), on_result=got.append, max_workers=8)

    async def drive():
        pipe.start(asyncio.get_running_loop())
        t0 = time.monotonic()
        for i in range(6):
            pipe.submit(ExecJob(f"M{i}", i, 3.0, 1.0,
                                [Event(ts=T0, kind="ENTER", price=1.0, frac=0.0, remaining_frac=1.0)], T0))
        for _ in range(300):
            if len(got) >= 6:
                break
            await asyncio.sleep(0.02)
        elapsed = time.monotonic() - t0
        pipe.stop()
        return elapsed

    elapsed = asyncio.run(drive())
    assert len(got) == 6
    assert elapsed < 3 * LAT, f"expected concurrent execution (~{LAT}s), took {elapsed:.2f}s (serial=~{6*LAT}s)"
