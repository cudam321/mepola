"""Unit + synthetic-equivalence tests for the config #1 state machine (no network).

Each scenario builds a synthetic candle series, runs it through both `TailRider` and
the golden `sim` oracle, and asserts (a) the realized multiple matches the oracle to
floating-point precision and (b) the lifecycle transitions are the ones config #1
specifies (dip entry, -30% stop pre-secure, 3x secure + stop removal, ride ladder).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from memebot.live.strategy import PositionState, TailRider, TailRiderConfig
from memebot.models import Candle

from sim_oracle import sim_multiple

T0 = datetime(2026, 6, 1, 0, 0, 0, tzinfo=timezone.utc)


def _c(i, o, h, l, cl, *, minutes=True) -> Candle:
    step = timedelta(minutes=i) if minutes else timedelta(hours=i)
    return Candle(ts=T0 + step, open=o, high=h, low=l, close=cl, volume=1.0)


def _run(candles) -> TailRider:
    tr = TailRider()
    for c in candles:
        tr.on_candle(c)
    tr.finalize(candles[-1].close, candles[-1].ts)
    return tr


def _oracle(candles):
    H = np.array([c.high for c in candles])
    L = np.array([c.low for c in candles])
    C = np.array([c.close for c in candles])
    T = np.array([c.ts.timestamp() for c in candles])
    return sim_multiple(H, L, C, T, candles[0].open)


def _assert_matches_oracle(candles, tr):
    expected = _oracle(candles)
    if expected is None:
        assert tr.realized_multiple is None
    else:
        assert tr.realized_multiple == pytest.approx(expected, rel=1e-12, abs=1e-12)


# --------------------------------------------------------------------------- #
def test_dip_never_arrives_expires():
    # sig=100 -> needs low <= 50 within 48h; it never dips that far.
    candles = [_c(i, 100, 110, 80, 100, minutes=False) for i in range(0, 60)]  # 60 hourly bars
    tr = _run(candles)
    assert tr.state is PositionState.EXPIRED
    assert tr.entry is None
    assert tr.realized_multiple is None
    _assert_matches_oracle(candles, tr)


def test_dip_after_48h_is_ignored():
    # A -50% dip that only appears AFTER 48h must not trigger an entry (window closed).
    candles = [_c(i, 100, 110, 80, 100, minutes=False) for i in range(0, 49)]
    candles.append(_c(49, 100, 110, 40, 45, minutes=False))  # deep dip at hour 49 (> 48h)
    tr = _run(candles)
    assert tr.state is PositionState.EXPIRED
    _assert_matches_oracle(candles, tr)


def test_entry_levels_and_stop_armed():
    # dip on candle 1; entry = 50 * 1.01 = 50.5; stop = 0.70 * 50.5 = 35.35
    candles = [
        _c(0, 100, 100, 100, 100),
        _c(1, 100, 100, 45, 60),   # low 45 <= 50 -> ENTER; 45 > 35.35 so no stop; high 60 < 151.5 no TP
        _c(2, 60, 60, 55, 58),
    ]
    tr = TailRider()
    for c in candles:
        tr.on_candle(c)
    assert tr.entry == pytest.approx(50.5)
    assert tr.stop_price == pytest.approx(0.70 * 50.5)
    assert tr.state is PositionState.ENTERED
    assert tr.secured is False


def test_pre_secure_stop_out():
    candles = [
        _c(0, 100, 100, 100, 100),
        _c(1, 100, 100, 48, 60),   # ENTER at 50.5 (dip to 48); 48 > 35.35 no stop yet
        _c(2, 60, 60, 30, 32),     # low 30 <= 35.35 -> STOP_OUT (unsecured)
        _c(3, 32, 40, 20, 25),
    ]
    tr = _run(candles)
    assert tr.state is PositionState.STOPPED
    assert tr.rem == 0.0
    # realized = 1.0 * (0.70*50.5) * 0.95 / 50.5 = 0.70*0.95 = 0.665
    assert tr.realized_multiple == pytest.approx(0.70 * 0.95)
    _assert_matches_oracle(candles, tr)


def test_intrabar_stop_before_tp():
    # A single bar spans BOTH the stop and the 3x rung -> pessimistic: stop wins.
    candles = [
        _c(0, 100, 100, 100, 100),
        _c(1, 100, 100, 50, 60),          # ENTER at 50.5 (dip to 50). 50 > 35.35 no stop.
        _c(2, 60, 200, 30, 40),           # high 200 >= 151.5 (3x) AND low 30 <= 35.35 (stop) -> STOP
    ]
    tr = _run(candles)
    assert tr.state is PositionState.STOPPED
    assert tr.secured is False
    assert tr.realized_multiple == pytest.approx(0.70 * 0.95)
    _assert_matches_oracle(candles, tr)


def test_secure_then_ride_ladder():
    # ENTER at 50.5; then a huge bar clears 3x,6x,12x,24x,48x,144x in one candle.
    # Rungs: 3->6->12->24->48 (x2 while ntp<5) then x3 -> 144.
    entry = 50.5
    candles = [
        _c(0, 100, 100, 100, 100),
        _c(1, 100, 100, 50, 60),                    # ENTER at 50.5
        _c(2, 60, 200 * entry, 55, 30 * entry),     # high >> 48x; clears many rungs
        _c(3, 30 * entry, 30 * entry, 29 * entry, 30 * entry),
    ]
    tr = _run(candles)
    assert tr.secured is True
    assert tr.stop_price is None                    # stop removed after securing
    assert tr.state in (PositionState.RIDING, PositionState.EXITED)
    assert tr.n_tp >= 5                              # at least 3,6,12,24,48 filled
    _assert_matches_oracle(candles, tr)


def test_ride_to_close_no_stop_after_secure():
    # After securing at 3x, a later dip below the old stop must NOT stop us out.
    entry = 50.5
    candles = [
        _c(0, 100, 100, 100, 100),
        _c(1, 100, 100, 50, 60),                       # ENTER at 50.5
        _c(2, 60, 4 * entry, 55, 3.5 * entry),         # clears 3x -> secure, sell 33%, stop removed
        _c(3, 3.5 * entry, 3.5 * entry, 0.1 * entry, 0.2 * entry),  # crashes below old stop; no stop now
    ]
    tr = _run(candles)
    assert tr.secured is True
    assert tr.state is PositionState.EXITED           # residual rode to the (low) close
    # residual was NOT stopped: it finalized at the last close (0.2*entry)
    _assert_matches_oracle(candles, tr)


def test_immediate_dip_on_first_candle():
    # The first candle itself dips to -50% (volatile launch minute) -> enter on candle 0.
    candles = [
        _c(0, 100, 120, 45, 60),   # sig=100; low 45 <= 50 -> ENTER on candle 0
        _c(1, 60, 60, 55, 58),
    ]
    tr = _run(candles)
    assert tr.entry == pytest.approx(50.5)
    assert tr.state in (PositionState.ENTERED, PositionState.SECURED, PositionState.RIDING, PositionState.EXITED)
    _assert_matches_oracle(candles, tr)


def test_snapshot_restore_roundtrip():
    candles = [
        _c(0, 100, 100, 100, 100),
        _c(1, 100, 100, 50, 60),
        _c(2, 60, 4 * 50.5, 55, 3.5 * 50.5),   # secure at 3x
    ]
    tr = TailRider()
    for c in candles:
        tr.on_candle(c)
    snap = tr.snapshot()
    tr2 = TailRider.restore(TailRiderConfig(), snap)
    assert tr2.snapshot() == snap
    # continue both and confirm they stay identical
    tail = _c(3, 3.5 * 50.5, 3.5 * 50.5, 0.1 * 50.5, 0.2 * 50.5)
    tr.on_candle(tail); tr.finalize(tail.close, tail.ts)
    tr2.on_candle(tail); tr2.finalize(tail.close, tail.ts)
    assert tr.realized_multiple == pytest.approx(tr2.realized_multiple)
