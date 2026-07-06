"""Executor tests: PaperExecutor faithfully realizes the machine's model (paper == backtest)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from memebot.live.executor import LiveExecutor, PaperExecutor
from memebot.live.state import LiveState
from memebot.live.strategy import TailRider, TailRiderConfig
from memebot.models import Candle

T0 = datetime(2026, 6, 1, 0, 0, 0, tzinfo=timezone.utc)


def _c(i, o, h, l, cl):
    return Candle(ts=T0 + timedelta(minutes=i), open=o, high=h, low=l, close=cl, volume=1.0)


def test_paper_entry_qty():
    px = PaperExecutor()
    fill = px.buy(mint="M", stake_usd=3.0, entry_price=50.5)
    assert fill.kind == "ENTRY"
    assert fill.tokens == pytest.approx(3.0 / 50.5)
    assert fill.usd == 3.0


def test_paper_sells_sum_to_realized_multiple():
    """Summing PaperExecutor USD proceeds across all events must equal stake * realized_multiple."""
    stake = 3.0
    candles = [
        _c(0, 100, 100, 100, 100),
        _c(1, 100, 100, 50, 60),                 # ENTER at 50.5
        _c(2, 60, 4 * 50.5, 55, 3.5 * 50.5),     # secure at 3x (sell 33%)
        _c(3, 3.5 * 50.5, 3.5 * 50.5, 0.2 * 50.5, 0.3 * 50.5),  # ride to close
    ]
    tr = TailRider()
    events = []
    for c in candles:
        events.extend(tr.on_candle(c))
    events.extend(tr.finalize(candles[-1].close, candles[-1].ts))

    entry = tr.entry
    px = PaperExecutor()
    total_usd = 0.0
    for ev in events:
        if ev.kind in ("TP", "RIDE_SELL", "STOP_OUT", "FINALIZE"):
            total_usd += px.sell_event(mint="M", stake_usd=stake, entry_price=entry, event=ev).usd

    assert total_usd == pytest.approx(stake * tr.realized_multiple, rel=1e-12)
    # and the realized pnl is stake*(mult-1)
    assert total_usd - stake == pytest.approx(stake * (tr.realized_multiple - 1), rel=1e-12)


def test_live_executor_is_gated(tmp_path):
    st = LiveState(tmp_path / "s.db")
    lx = LiveExecutor(st, None, TailRiderConfig(), armed=False)   # inert by default
    with pytest.raises(PermissionError):
        lx.buy(mint="M", stake_usd=3.0, entry_price=1.0)
    # even "armed" but mode still paper -> refuses
    lx.armed = True
    with pytest.raises(PermissionError):
        lx.buy(mint="M", stake_usd=3.0, entry_price=1.0)
    st.close()
