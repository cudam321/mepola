"""Manual override layer — the NEW model (2026-07-06): the algo manages everything by default; the
human OVERRIDES per-token. A DIRECT BUY creates an ALGO-managed position (config #1 rides it); a
manual SELL / TP / SL implicitly TAKES OVER that one position; ADD WATCHLIST injects a signal.

Paper mode, offline. The live off-loop path shares the same booking."""

from __future__ import annotations

import pytest

from memebot.live.engine import LiveEngine
from memebot.live.risk import RiskConfig, RiskGovernor
from memebot.live.state import LiveState

MINT = "MintManual1111111111111111111111111111111111"
MINT2 = "Mint2xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"


def _engine(tmp_path):
    st = LiveState(tmp_path / "s.db")
    risk = RiskGovernor(st, RiskConfig(stake_usd=3.0))
    return st, LiveEngine(st, risk)


# ---- DIRECT BUY → algo-managed position ------------------------------------- #
def test_direct_buy_creates_algo_position_with_rider(tmp_path):
    st, eng = _engine(tmp_path)
    ok, msg = eng.direct_buy(MINT, usd=5.0, price=2.0, ticker="TOK")
    assert ok, msg
    pos = st.get_position(MINT)
    assert pos["controller"] == "algo"                 # the algo rides it (config #1)
    assert pos["state"] == "ENTERED"
    assert pos["stake_usd"] == 5.0
    assert pos["tokens_qty"] == pytest.approx(2.5)     # 5 / 2
    assert pos["entry_price"] == pytest.approx(2.0)
    assert pos["stop_price"] == pytest.approx(0.70 * 2.0)   # −30% stop armed from the buy price
    assert MINT in eng.riders and MINT not in eng.manual_pids
    assert st.is_seen(MINT)


def test_direct_buy_then_algo_secures_at_3x(tmp_path):
    """A direct buy is genuinely algo-driven: feeding candles runs config #1 (3× secure)."""
    from memebot.models import Candle
    from datetime import timedelta
    from memebot.live.state import utcnow
    st, eng = _engine(tmp_path)
    # anchor the buy explicitly so the candle is provably after the rider's t0 (the on_candle guard
    # drops pre-signal candles) — no dependency on the wall clock vs a hardcoded date.
    t0 = utcnow()
    eng.direct_buy(MINT, usd=3.0, price=1.0, ticker="TOK", ts=t0)
    eng.on_candle(MINT, Candle(ts=t0 + timedelta(seconds=1), open=1.0, high=3.2, low=1.0,
                               close=3.1, volume=1.0))
    pos = st.get_position(MINT)
    assert pos["state"] == "SECURED"                   # algo hit the 3× rung and secured
    assert pos["n_tp"] == 1


def test_hard_cap_clamps_direct_buy(tmp_path):
    st, eng = _engine(tmp_path)
    st.set_system("manual_trade_hard_cap_usd", "10.0")
    ok, _ = eng.direct_buy(MINT, usd=999.0, price=2.0)
    assert ok
    assert st.get_position(MINT)["stake_usd"] == 10.0


def test_kill_switch_blocks_direct_buy(tmp_path):
    st, eng = _engine(tmp_path)
    st.set_system("kill_switch", "on")
    ok, reason = eng.direct_buy(MINT, usd=5.0, price=2.0)
    assert not ok and "kill" in reason.lower()


def test_cap_zero_disables_direct_buys(tmp_path):
    st, eng = _engine(tmp_path)
    st.set_system("manual_cap_usd", "0")
    ok, reason = eng.direct_buy(MINT, usd=3.0, price=1.0)
    assert not ok and "disabled" in reason.lower()


# ---- ADD WATCHLIST → inject a signal ---------------------------------------- #
def test_inject_signal_creates_watching_algo_position(tmp_path):
    st, eng = _engine(tmp_path)
    ok, msg = eng.inject_signal(MINT, price=1.0, ticker="TOK")
    assert ok and msg == "watching"
    pos = st.get_position(MINT)
    assert pos["state"] == "WATCHING" and pos["controller"] == "algo"
    assert pos["signal_price"] == pytest.approx(1.0)
    assert MINT in eng.riders                          # algo watches for the −50% dip
    # injecting the same mint again is a no-op
    assert not eng.inject_signal(MINT, price=1.0)[0]


# ---- OVERRIDE: manual sell implicitly takes over ---------------------------- #
def test_sell_overrides_algo_position_implicit_takeover(tmp_path):
    st, eng = _engine(tmp_path)
    eng.direct_buy(MINT, usd=10.0, price=1.0, ticker="TOK")     # algo position, 10 tokens
    assert MINT in eng.riders
    ok, _ = eng.manual_sell(MINT, price=2.0, sell_frac=0.5)     # override → take over → sell 50%
    assert ok
    pos = st.get_position(MINT)
    assert pos["controller"] == "manual"               # implicit take-over
    assert MINT not in eng.riders and MINT in eng.manual_pids
    assert pos["remaining_frac"] == pytest.approx(0.5)
    assert pos["proceeds_units"] == pytest.approx(1.0)         # 5 tokens*$2 / 10


def test_partial_then_full_close_realizes_real_dollars(tmp_path):
    st, eng = _engine(tmp_path)
    eng.direct_buy(MINT, usd=10.0, price=1.0, ticker="TOK")     # 10 tokens @ $1
    eng.manual_sell(MINT, price=2.0, sell_frac=0.5)            # sell half @ $2 -> $10
    ok, _ = eng.manual_sell(MINT, price=3.0, close=True)       # close rest (5 @ $3 -> $15)
    assert ok
    closed = st.closed_trades()
    assert len(closed) == 1
    assert closed[0]["realized_multiple"] == pytest.approx(2.5)   # $25 / $10
    assert closed[0]["pnl_usd"] == pytest.approx(15.0)
    assert st.get_position(MINT)["state"] == "EXITED"


def test_stop_sell_marks_stopped(tmp_path):
    st, eng = _engine(tmp_path)
    eng.direct_buy(MINT, usd=10.0, price=1.0, ticker="TOK")
    ok, _ = eng.manual_sell(MINT, price=0.7, close=True, is_stop=True)
    assert ok
    closed = st.closed_trades()[0]
    assert closed["was_stopped"] == 1 and closed["realized_multiple"] == pytest.approx(0.7)


def test_release_hands_back_to_algo(tmp_path):
    st, eng = _engine(tmp_path)
    eng.direct_buy(MINT, usd=10.0, price=1.0, ticker="TOK")
    eng.manual_sell(MINT, price=2.0, sell_frac=0.25)          # take over
    assert st.get_position(MINT)["controller"] == "manual"


def test_finalize_manual_dead_token_total_loss(tmp_path):
    st, eng = _engine(tmp_path)
    eng.direct_buy(MINT, usd=10.0, price=1.0, ticker="TOK")
    eng.manual_sell(MINT, price=1.0, sell_frac=0.1)           # take over (now manual)
    assert st.get_position(MINT)["controller"] == "manual"
    eng.finalize_manual(MINT, 0.0)                            # rugged -> residual ~0
    closed = st.closed_trades()[0]
    assert closed["realized_multiple"] == pytest.approx(0.1)  # only the 10% sold at $1 recovered
    assert MINT not in eng.manual_pids
    assert st.get_position(MINT)["state"] == "EXITED"


def test_sell_without_position_rejected(tmp_path):
    st, eng = _engine(tmp_path)
    ok, _ = eng.manual_sell(MINT, price=1.0, close=True)
    assert not ok


# ---- LIVE off-loop path (through the real pipeline, fake executor) ---------- #
from memebot.live.execution import FillResult, LegResult, LiveExecutionPipeline   # noqa: E402
from memebot.live.executor import Fill                                            # noqa: E402
from memebot.live.strategy import Event                                           # noqa: E402


class _ManualFakeExec:
    mode = "live"

    def __init__(self):
        self.armed = True
        self.dry_run = True

    def buy(self, *, mint, stake_usd, entry_price, ts=None):
        tokens = stake_usd / entry_price
        return Fill(mint, "ENTRY", entry_price, tokens, stake_usd, ts=ts, note="fake live buy")

    def sell_event(self, *, mint, stake_usd, entry_price, event, tokens_qty=None,
                   target_remaining_tokens=None):
        held = tokens_qty if tokens_qty is not None else 0.0
        target = target_remaining_tokens if target_remaining_tokens is not None else 0.0
        sold = max(0.0, held - target)
        return Fill(mint, "SELL", event.price, sold, sold * event.price, ts=event.ts, note="fake sell")


def _live_engine(tmp_path):
    st = LiveState(tmp_path / "s.db")
    st.set_system("mode", "live")
    st.set_system("kill_switch", "off")
    fe = _ManualFakeExec()
    pipe = LiveExecutionPipeline(fe, on_result=lambda r: None)
    eng = LiveEngine(st, RiskGovernor(st, RiskConfig(stake_usd=3.0)), executor=fe, pipeline=pipe)
    pipe.on_result = eng.apply_fill_result
    return st, eng, pipe


def _drain(pipe, eng):
    while not pipe._q.empty():
        eng.apply_fill_result(pipe.execute(pipe._q.get_nowait()))


def test_live_direct_buy_books_algo_after_confirm(tmp_path):
    st, eng, pipe = _live_engine(tmp_path)
    ok, msg = eng.direct_buy(MINT, usd=6.0, price=2.0, ticker="TOK")
    assert ok and msg == "submitted"
    assert st.get_position(MINT)["state"] == "WATCHING"        # not booked until confirm
    _drain(pipe, eng)
    pos = st.get_position(MINT)
    assert pos["state"] == "ENTERED" and pos["controller"] == "algo"
    assert MINT in eng.riders and pos["tokens_qty"] == pytest.approx(3.0)


def test_live_override_sell_takes_over_and_closes(tmp_path):
    st, eng, pipe = _live_engine(tmp_path)
    eng.direct_buy(MINT, usd=10.0, price=1.0, ticker="TOK"); _drain(pipe, eng)
    assert MINT in eng.riders
    eng.manual_sell(MINT, price=3.0, close=True); _drain(pipe, eng)
    assert st.get_position(MINT)["state"] == "EXITED"
    assert MINT not in eng.riders


def test_direct_buy_respects_deployed_cap(tmp_path):
    # BLOCKER #1: a direct buy books controller='algo', so it MUST honor total_deployed_cap_usd via
    # can_enter (not just the per-order clamp). Two $10 buys fill; the third breaches $25 and rejects.
    st, eng, pipe = _live_engine(tmp_path)
    st.set_system("ctl_total_deployed_cap_usd", "25")
    ok, _ = eng.direct_buy(MINT, usd=10.0, price=1.0); _drain(pipe, eng)      # deployed 10
    assert ok and st.get_position(MINT)["state"] == "ENTERED"
    ok, _ = eng.direct_buy(MINT2, usd=10.0, price=1.0); _drain(pipe, eng)     # deployed 20
    assert ok and st.get_position(MINT2)["state"] == "ENTERED"
    ok, reason = eng.direct_buy("Mint3zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz",
                                usd=10.0, price=1.0)                          # 20+10 > 25
    assert not ok and "risk cap" in reason
    assert eng.state.get_position("Mint3zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz") is None  # no orphan row


def test_enter_rechecks_deployed_cap_at_commit(tmp_path):
    # BLOCKER #2: caps bind at the capital-committing ENTER, not only at signal-ingest. A WATCHING
    # position that dips -50% while the deployed cap is full must NOT enter — it stays WATCHING.
    from datetime import timedelta
    from memebot.models import Candle, Signal, SignalSide
    from memebot.live.state import utcnow
    st, eng, pipe = _live_engine(tmp_path)
    st.set_system("ctl_total_deployed_cap_usd", "5")            # room for ~one $3 stake
    t0 = utcnow()
    # 1) admit the watcher WHILE deployed is $0 (this is how watchers accumulate over time)
    sig = Signal(source_channel="test", message_id=1, posted_at=t0, raw_text="call",
                 side=SignalSide.BUY, mint=MINT, ticker="TOK", parse_confidence=1.0)
    assert eng.ingest_call(sig, price=1.0, now=t0)               # WATCHING (no capital yet)
    assert st.get_position(MINT)["state"] == "WATCHING"
    # 2) NOW fill the deployed cap with another entry (the correlated-market situation)
    eng.direct_buy(MINT2, usd=3.0, price=1.0); _drain(pipe, eng)   # deployed -> 3, cap 5
    assert st.get_position(MINT2)["state"] == "ENTERED"
    # 3) the watcher dips -50% -> would ENTER ($3) -> 3+3 > 5 -> must be blocked at commit
    eng.on_candle(MINT, Candle(ts=t0 + timedelta(seconds=1), open=1.0, high=1.0, low=0.4,
                               close=0.5, volume=1.0))
    _drain(pipe, eng)
    assert st.get_position(MINT)["state"] == "WATCHING"          # blocked by the deployed cap at ENTER
    assert MINT not in eng._pending


def test_cap_binds_under_burst_without_draining(tmp_path):
    # audit RE-VERIFY #1/#2: a correlated dip fires N buys in ONE sweep BEFORE any confirms. The
    # in-flight reservation (engine._pending_buy_usd -> can_enter reserved_usd) must make the cap bind;
    # a confirmed-only count would let them all pass and overshoot (empirically an 80% overshoot).
    st, eng, pipe = _live_engine(tmp_path)
    st.set_system("ctl_total_deployed_cap_usd", "5")            # room for ~one $3 stake
    r1 = eng.direct_buy(MINT, usd=3.0, price=1.0)               # 0 + 3 <= 5 -> submitted
    r2 = eng.direct_buy(MINT2, usd=3.0, price=1.0)              # reserved 3 + 3 > 5 -> rejected
    r3 = eng.direct_buy("Mint3zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz", usd=3.0, price=1.0)
    assert r1[0] and not r2[0] and not r3[0]
    assert "risk cap" in r2[1]
    _drain(pipe, eng)                                          # only the one that passed commits
    deployed = st.query("SELECT COALESCE(SUM(stake_usd),0) AS d FROM positions "
                        "WHERE state IN ('ENTERED','SECURED','RIDING')")[0]["d"]
    assert deployed <= 5.0                                     # NO overshoot


def test_takeover_during_pending_sell_still_books_the_fill(tmp_path):
    # audit RE-VERIFY #10 (regression): a dashboard take-over (controller='manual') while an algo TP is
    # in-flight must NOT drop the confirmed fill — on_candle must buffer, not pop the rider, while pending.
    from datetime import timedelta
    from memebot.models import Candle
    from memebot.live.state import utcnow
    st, eng, pipe = _live_engine(tmp_path)
    t0 = utcnow()
    eng.direct_buy(MINT, usd=10.0, price=1.0, ticker="TOK", ts=t0); _drain(pipe, eng)  # ENTERED @1, 10 tok
    eng.on_candle(MINT, Candle(ts=t0 + timedelta(seconds=1), open=1.0, high=3.2, low=1.0,
                               close=3.1, volume=1.0))          # clears 3x -> TP submitted, MINT _pending
    assert MINT in eng._pending
    st.update_position(MINT, controller="manual")               # take-over out-of-band WHILE pending
    eng.on_candle(MINT, Candle(ts=t0 + timedelta(seconds=2), open=3.1, high=3.1, low=3.0,
                               close=3.05, volume=1.0))          # a tick arrives before the swap confirms
    assert MINT in eng.riders                                    # rider preserved (NOT dropped) while pending
    _drain(pipe, eng)                                           # TP confirms
    pos = st.get_position(MINT)
    assert pos["n_tp"] == 1 and pos["secured"]                  # the TP was booked, not lost
    assert pos["remaining_frac"] < 1.0


def test_reconcile_landed_algo_sell_books_real_proceeds(tmp_path):
    # audit RE-VERIFY #5: a landed TP whose loop-apply was lost -> reconcile re-drives the rider through
    # the leg and books REAL proceeds (not the $0 the idempotent retry would book).
    from memebot.live.state import utcnow
    st, eng, pipe = _live_engine(tmp_path)
    eng.direct_buy(MINT, usd=10.0, price=1.0, ticker="TOK", ts=utcnow()); _drain(pipe, eng)
    pos = st.get_position(MINT)
    tokens = pos["tokens_qty"]
    assert pos["state"] == "ENTERED" and pos["remaining_frac"] == 1.0
    # did-not-land: bag unchanged -> False, no change
    assert eng.reconcile_landed_algo_sell(MINT, "TP", 3.0, tokens) is False
    assert st.get_position(MINT)["remaining_frac"] == 1.0
    # landed: bag shrank to 67% (TP1 sold 33%) -> re-drive + book proceeds
    assert eng.reconcile_landed_algo_sell(MINT, "TP", 3.0, tokens * 0.67) is True
    pos = st.get_position(MINT)
    assert pos["n_tp"] == 1 and pos["secured"]                  # advanced through the TP leg
    prow = st.query("SELECT COALESCE(SUM(proceeds_usd),0) AS p FROM position_events "
                    "WHERE position_id=? AND event_type='TP'", (pos["id"],))
    assert prow[0]["p"] > 0                                      # REAL proceeds booked, not $0


def test_dead_writeoff_closes_unroutable_position(tmp_path):
    # audit #7: a rugged/unroutable algo position is written off with NO swap — closed EXITED, real
    # loss booked, slot freed, and labeled 'dead_writeoff' consistently on positions AND closed_trades.
    from memebot.live.state import utcnow
    st, eng, pipe = _live_engine(tmp_path)
    eng.direct_buy(MINT, usd=10.0, price=1.0, ticker="TOK", ts=utcnow()); _drain(pipe, eng)
    pid = st.get_position(MINT)["id"]
    assert MINT in eng.riders
    eng._dead_writeoff(MINT, pid, last_price=0.0001)            # dead -> residual ≈ 0 (total loss)
    pos = st.get_position(MINT)
    assert pos["state"] == "EXITED" and pos["close_reason"] == "dead_writeoff"
    assert MINT not in eng.riders                               # slot freed
    ct = st.query("SELECT close_reason, pnl_usd FROM closed_trades WHERE position_id=?", (pid,))
    assert ct and ct[0]["close_reason"] == "dead_writeoff"      # labels consistent
    assert ct[0]["pnl_usd"] < 0                                 # unsecured writeoff = a real loss


def test_failed_override_order_resets_to_open_for_retry(tmp_path):
    # a transient swap failure on a manual stop must reset to 'open' to RETRY, never 'failed'
    from memebot.live.state import utcnow
    st, eng = _engine(tmp_path)
    eng.direct_buy(MINT, usd=10.0, price=1.0, ticker="TOK")
    eng.manual_sell(MINT, price=1.0, sell_frac=0.1)           # take over -> manual
    pid = st.get_position(MINT)["id"]
    oid = st.create_order(mint=MINT, kind="stop_loss", side="sell",
                          trigger_type="price_at_or_below", trigger_value=0.7,
                          size_kind="token_frac", size_value=1.0, status="submitted")
    eng._manual_pending[MINT] = {"op": "sell", "order_id": oid, "is_stop": True, "note": ""}
    eng._pending.add(MINT)
    failed = FillResult(MINT, pid, False,
                        [LegResult(Event(utcnow(), "MANUAL_SELL"), False, None, "not confirmed")],
                        manual=True, order_id=oid)
    eng.apply_fill_result(failed)
    assert st.get_order(oid)["status"] == "open"
    assert MINT not in eng._pending
