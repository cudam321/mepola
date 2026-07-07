"""OrderBook (new model): a buy order → a DIRECT BUY (algo-managed position); a TP/SL/trailing sell
order fires → an OVERRIDE that implicitly takes the position over, then sells. Paper, offline."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from memebot.live.engine import LiveEngine
from memebot.live.orders import OrderBook
from memebot.live.risk import RiskConfig, RiskGovernor
from memebot.live.state import LiveState
from memebot.models import Candle

T0 = datetime(2026, 7, 6, 0, 0, tzinfo=timezone.utc)
MINT = "MintOrders11111111111111111111111111111111111"


def _c(i, o, h, l, cl):
    return Candle(ts=T0 + timedelta(minutes=i), open=o, high=h, low=l, close=cl, volume=1.0)


def _setup(tmp_path):
    st = LiveState(tmp_path / "s.db")
    risk = RiskGovernor(st, RiskConfig(stake_usd=3.0))
    eng = LiveEngine(st, risk)
    return st, eng, OrderBook(st, eng)


def test_market_buy_order_creates_algo_position(tmp_path):
    st, eng, ob = _setup(tmp_path)
    oid = st.create_order(mint=MINT, ticker="TOK", kind="market", side="buy",
                          trigger_type="now", size_kind="usd", size_value=5.0)
    ob.on_candle(MINT, _c(0, 2.0, 2.0, 2.0, 2.0))
    assert st.get_order(oid)["status"] == "filled"
    pos = st.get_position(MINT)
    assert pos["controller"] == "algo" and pos["state"] == "ENTERED"      # algo rides it
    assert pos["stake_usd"] == 5.0 and pos["entry_price"] == pytest.approx(2.0)
    assert MINT in eng.riders


def test_limit_buy_waits_then_fires_algo(tmp_path):
    st, eng, ob = _setup(tmp_path)
    oid = st.create_order(mint=MINT, ticker="TOK", kind="limit", side="buy",
                          trigger_type="price_at_or_below", trigger_value=1.0,
                          size_kind="usd", size_value=3.0)
    ob.on_candle(MINT, _c(0, 2.0, 2.2, 1.5, 2.0))     # low 1.5 > 1.0 -> no fill
    assert st.get_order(oid)["status"] == "open"
    ob.on_candle(MINT, _c(1, 1.5, 1.6, 0.9, 1.1))     # low 0.9 <= 1.0 -> fills @ 1.0, algo position
    assert st.get_order(oid)["status"] == "filled"
    assert st.get_position(MINT)["controller"] == "algo"


def test_take_profit_override_takes_over_and_sells(tmp_path):
    st, eng, ob = _setup(tmp_path)
    eng.direct_buy(MINT, usd=10.0, price=1.0, ticker="TOK")               # algo position, 10 tokens
    oid = st.create_order(mint=MINT, ticker="TOK", kind="take_profit", side="sell",
                          trigger_type="mult_at_or_above", trigger_value=3.0,
                          size_kind="token_frac", size_value=0.5)
    ob.on_candle(MINT, _c(0, 1.0, 2.5, 1.0, 2.2))     # high 2.5 < 3x -> no
    assert st.get_order(oid)["status"] == "open"
    ob.on_candle(MINT, _c(1, 2.5, 3.5, 2.5, 3.2))     # high >= 3x -> fire -> take over -> sell 50%
    assert st.get_order(oid)["status"] == "filled"
    pos = st.get_position(MINT)
    assert pos["controller"] == "manual"              # override took it over
    assert pos["remaining_frac"] == pytest.approx(0.5)
    assert MINT not in eng.riders


def test_stop_loss_fires_before_take_profit_same_bar(tmp_path):
    st, eng, ob = _setup(tmp_path)
    eng.direct_buy(MINT, usd=10.0, price=1.0, ticker="TOK")
    tp = st.create_order(mint=MINT, kind="take_profit", side="sell",
                         trigger_type="price_at_or_above", trigger_value=3.0,
                         size_kind="token_frac", size_value=0.5)
    sl = st.create_order(mint=MINT, kind="stop_loss", side="sell",
                         trigger_type="price_at_or_below", trigger_value=0.7,
                         size_kind="token_frac", size_value=1.0)
    ob.on_candle(MINT, _c(0, 1.0, 3.5, 0.6, 1.0))     # hits both -> the STOP wins (pessimistic)
    assert st.get_order(sl)["status"] == "filled"
    assert st.get_order(tp)["status"] == "cancelled"  # closing cancels the sibling
    assert st.closed_trades()[0]["was_stopped"] == 1


def test_trailing_stop_holds_then_fires(tmp_path):
    st, eng, ob = _setup(tmp_path)
    eng.direct_buy(MINT, usd=10.0, price=1.0, ticker="TOK")
    oid = st.create_order(mint=MINT, kind="trailing_stop", side="sell",
                          trigger_type="peak_drawdown_pct", trigger_value=0.25,
                          size_kind="token_frac", size_value=1.0)
    ob.on_candle(MINT, _c(0, 1.0, 2.0, 1.6, 1.9))     # hwm 2.0, level 1.5, low 1.6 -> hold
    assert st.get_order(oid)["status"] == "open"
    ob.on_candle(MINT, _c(1, 1.9, 4.0, 3.2, 3.9))     # hwm 4.0, level 3.0, low 3.2 -> hold
    assert st.get_order(oid)["status"] == "open" and st.get_order(oid)["hwm"] == pytest.approx(4.0)
    ob.on_candle(MINT, _c(2, 3.9, 4.0, 2.9, 3.0))     # low 2.9 <= 3.0 -> fire
    assert st.get_order(oid)["status"] == "filled"


def test_order_expires(tmp_path):
    st, eng, ob = _setup(tmp_path)
    oid = st.create_order(mint=MINT, kind="limit", side="buy",
                          trigger_type="price_at_or_below", trigger_value=1.0,
                          size_kind="usd", size_value=3.0,
                          expires_at=datetime(2020, 1, 1, tzinfo=timezone.utc))   # long past
    ob.on_candle(MINT, _c(0, 0.5, 0.5, 0.5, 0.5))
    assert st.get_order(oid)["status"] == "expired"
    assert st.get_position(MINT) is None


def test_one_order_per_candle(tmp_path):
    st, eng, ob = _setup(tmp_path)
    eng.direct_buy(MINT, usd=10.0, price=1.0, ticker="TOK")
    o1 = st.create_order(mint=MINT, kind="take_profit", side="sell",
                         trigger_type="price_at_or_above", trigger_value=2.0,
                         size_kind="token_frac", size_value=0.25)
    o2 = st.create_order(mint=MINT, kind="take_profit", side="sell",
                         trigger_type="price_at_or_above", trigger_value=2.0,
                         size_kind="token_frac", size_value=0.25)
    ob.on_candle(MINT, _c(0, 1.0, 2.5, 1.0, 2.2))     # both trigger; only ONE fires this bar
    assert sorted([st.get_order(o1)["status"], st.get_order(o2)["status"]]) == ["filled", "open"]
    ob.on_candle(MINT, _c(1, 2.2, 2.5, 2.0, 2.3))
    assert st.get_order(o1)["status"] == "filled" and st.get_order(o2)["status"] == "filled"


# ---- take-over / release reconcile on the orchestrator ---------------------- #
def _paper_orch(db):
    """An Orchestrator pinned to PAPER mode — hermetic: these tests exercise the inline paper
    path and must not flip behavior when the operator arms the repo config to mode=live."""
    from memebot.config import Settings
    from memebot.live.run import Orchestrator
    s = Settings.load()
    s.raw.setdefault("strategy", {}).setdefault("tailrider", {})["mode"] = "paper"
    return Orchestrator(db, settings=s)


def test_release_hands_back_to_algo_reconcile(tmp_path):
    from memebot.live.strategy import PositionState
    orch = _paper_orch(tmp_path / "s.db")
    try:
        orch.engine.direct_buy(MINT, usd=10.0, price=1.0, ticker="TOK")   # algo position + rider
        orch.engine.manual_sell(MINT, price=2.0, sell_frac=0.25)          # override -> manual
        assert MINT in orch.engine.manual_pids and MINT not in orch.engine.riders
        # release back to the algo
        orch.state.update_position(MINT, controller="algo")
        orch.state.set_system("controller_rev",
                              str(int(orch.state.get_system("controller_rev") or "0") + 1))
        orch._reconcile_controllers()
        assert MINT in orch.engine.riders and MINT not in orch.engine.manual_pids
    finally:
        orch.state.close()


def test_takeover_deferred_while_swap_in_flight(tmp_path):
    from memebot.models import Signal, SignalSide
    from memebot.live.strategy import PositionState
    orch = _paper_orch(tmp_path / "s.db")
    try:
        sig = Signal(source_channel="c", message_id=1, posted_at=T0, raw_text="b",
                     side=SignalSide.BUY, mint=MINT, ticker="TOK", parse_confidence=1.0)
        assert orch.engine.ingest_call(sig, price=100.0, now=T0)
        orch.engine.on_candle(MINT, _c(1, 100, 100, 40, 60))
        assert orch.engine.riders[MINT].state is PositionState.ENTERED
        orch.engine._pending.add(MINT)                 # a swap is in flight
        orch.state.update_position(MINT, controller="manual")
        orch.state.set_system("controller_rev", "1")
        orch._reconcile_controllers()
        assert MINT in orch.engine.riders              # DEFERRED (rider not dropped)
        assert orch._controller_rev != "1"
        orch.engine._pending.discard(MINT)
        orch._reconcile_controllers()
        assert MINT not in orch.engine.riders and MINT in orch.engine.manual_pids
    finally:
        orch.state.close()


def test_release_fast_forwards_the_ladder_no_catchup_sells(tmp_path):
    """ladder-replay incident (2026-07-07): releasing a SECURED manual position whose price
    sits far above the next rung must NOT replay the missed rungs at market — the ladder
    resumes ABOVE the current price and nothing sells until a NEW high."""
    orch = _paper_orch(tmp_path / "s.db")
    try:
        orch.engine.direct_buy(MINT, usd=10.0, price=1.0, ticker="TOK")
        orch.engine.manual_sell(MINT, price=2.0, sell_frac=0.25)          # -> manual controller
        # a secured bag whose ladder state was lost (NULL next rung), price now 40x entry
        orch.state.update_position(MINT, secured=1, n_tp=1, next_rung_mult=None,
                                   current_price=40.0)
        orch.state.update_position(MINT, controller="algo")
        orch.state.set_system("controller_rev",
                              str(int(orch.state.get_system("controller_rev") or "0") + 1))
        orch._reconcile_controllers()
        tr = orch.engine.riders[MINT]
        assert tr.lvl * tr.entry > 40.0                    # ladder resumed ABOVE the market
        rem_before = orch.state.get_position(MINT)["remaining_frac"]
        orch.engine.on_candle(MINT, _c(2, 40.0, 40.0, 39.0, 40.0))
        assert orch.state.get_position(MINT)["remaining_frac"] == rem_before   # no catch-up sell
        evs = orch.state.query("SELECT event_type FROM position_events WHERE mint=?", (MINT,))
        assert "RIDE_SELL" not in [e["event_type"] for e in evs]
    finally:
        orch.state.close()


# -- H3 (audit 2026-07-07): cancel/fire compare-and-swap across processes ------------ #
def test_cancelled_order_never_fires_even_from_a_stale_snapshot(tmp_path):
    """The dashboard (another process) cancels between the engine's open_orders read and the
    fire — the status claim must make the cancel win."""
    st, eng, ob = _setup(tmp_path)
    eng.direct_buy(MINT, usd=10.0, price=1.0, ticker="TOK")
    oid = st.create_order(mint=MINT, kind="stop_loss", side="sell",
                          trigger_type="price_at_or_below", trigger_value=0.7,
                          size_kind="token_frac", size_value=1.0)
    st.claim_order(oid, "open", "cancelled", note="cancelled by user")   # user got there first
    ok, msg = eng.manual_sell(MINT, price=0.65, order_id=oid, is_stop=True, close=True)
    assert not ok and "no longer open" in msg
    assert st.get_order(oid)["status"] == "cancelled"
    assert st.get_position(MINT)["remaining_frac"] in (None, 1.0)        # nothing sold
    st.close()


def test_claim_order_cas_semantics(tmp_path):
    st, eng, ob = _setup(tmp_path)
    oid = st.create_order(mint=MINT, kind="stop_loss", side="sell",
                          trigger_type="price_at_or_below", trigger_value=0.7,
                          size_kind="token_frac", size_value=1.0)
    assert st.claim_order(oid, "open", "submitted")          # first claim wins
    assert not st.claim_order(oid, "open", "cancelled")      # a stale cancel loses
    assert st.get_order(oid)["status"] == "submitted"
    # the in-flight failure reset respects a cancel that happened during the flight
    assert st.claim_order(oid, "submitted", "cancelled", note="cancelled by user")
    assert not st.claim_order(oid, "submitted", "open", note="retrying after failure")
    assert st.get_order(oid)["status"] == "cancelled"        # NOT resurrected
    st.close()
