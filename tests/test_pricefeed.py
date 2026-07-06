"""Price-feed tests — tick emission / dead detection (fake client) + the pure candle aggregator."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from memebot.live.pricefeed import CandleAggregator, PriceFeed
from memebot.live.strategy import PositionState, TailRider, TailRiderConfig
from memebot.models import Candle

T0 = datetime(2026, 6, 1, 0, 0, 0, tzinfo=timezone.utc)


def test_aggregator_builds_ohlc_and_rolls():
    agg = CandleAggregator()
    # minute 0: ticks 100, 120, 90, 110  -> O100 H120 L90 C110
    assert agg.add_tick("M", 100, T0 + timedelta(seconds=1)) is None
    assert agg.add_tick("M", 120, T0 + timedelta(seconds=20)) is None
    assert agg.add_tick("M", 90, T0 + timedelta(seconds=40)) is None
    assert agg.add_tick("M", 110, T0 + timedelta(seconds=55)) is None
    # first tick of minute 1 finalizes minute 0's bar
    bar = agg.add_tick("M", 130, T0 + timedelta(seconds=61))
    assert bar is not None
    assert (bar.open, bar.high, bar.low, bar.close) == (100, 120, 90, 110)
    assert bar.ts == T0


def test_flush_emits_partial_bar():
    agg = CandleAggregator()
    agg.add_tick("M", 100, T0 + timedelta(seconds=1))
    agg.add_tick("M", 200, T0 + timedelta(seconds=2))
    bar = agg.flush("M")
    assert bar is not None and bar.high == 200 and bar.low == 100
    assert agg.flush("M") is None  # nothing left


def test_per_mint_isolation():
    agg = CandleAggregator()
    agg.add_tick("A", 1.0, T0 + timedelta(seconds=1))
    agg.add_tick("B", 5.0, T0 + timedelta(seconds=1))
    a = agg.flush("A"); b = agg.flush("B")
    assert a.close == 1.0 and b.close == 5.0


# --------------------------------------------------------------------------- #
# Tick-driven PriceFeed (fake Jupiter client, no network)
# --------------------------------------------------------------------------- #

class FakeJupiter:
    """Exposes .price(mints) -> dict like JupiterClient. Set .prices per poll."""

    def __init__(self):
        self.prices: dict[str, float] = {}

    def price(self, mints):
        return {m: self.prices[m] for m in mints if m in self.prices}


def test_pricefeed_emits_tick_per_poll_and_fires_dead_after_time():
    """Dead detection is TIME-based (F24): a priced mint absent for >= dead_after_s of
    continuous wall-clock time fires on_dead — a short poll gap does NOT."""
    ticks, deaths = [], []
    jc = FakeJupiter()
    feed = PriceFeed(jc,
                     on_tick=lambda mint, price, ts: ticks.append((mint, price, ts)),
                     on_dead=lambda mint, price: deaths.append((mint, price)),
                     dead_after_s=5.0)
    feed.track("M")

    jc.prices = {"M": 1.5}
    asyncio.run(feed._poll_once(T0))
    jc.prices = {"M": 1.6}
    asyncio.run(feed._poll_once(T0 + timedelta(seconds=1)))
    assert ticks == [("M", 1.5, T0), ("M", 1.6, T0 + timedelta(seconds=1))]
    assert deaths == []

    # absent, but < dead_after_s of wall-time -> NOT dead yet (this is the F24 fix: a ~44s
    # blip that used to forfeit a live RIDING position now cannot finalize it)
    jc.prices = {}
    asyncio.run(feed._poll_once(T0 + timedelta(seconds=2)))   # absent_since = T0+2s
    asyncio.run(feed._poll_once(T0 + timedelta(seconds=4)))
    assert deaths == []
    # >= dead_after_s of continuous absence -> dead at the last seen price
    asyncio.run(feed._poll_once(T0 + timedelta(seconds=8)))
    assert deaths == [("M", 1.6)]
    assert ticks == [("M", 1.5, T0), ("M", 1.6, T0 + timedelta(seconds=1))]  # no extra ticks


def test_dead_guard_never_priced_mint_is_not_finalized():
    """A brand-new mint Jupiter hasn't indexed must NOT be declared dead; only a mint that
    has been priced at least once can be finalized (the 48h dip expiry owns the rest)."""
    ticks, deaths = [], []
    jc = FakeJupiter()
    feed = PriceFeed(jc,
                     on_tick=lambda mint, price, ts: ticks.append((mint, price, ts)),
                     on_dead=lambda mint, price: deaths.append((mint, price)),
                     dead_after_s=5.0)
    feed.track("NEW")

    # never priced: absent far longer than dead_after_s -> still NOT dead
    jc.prices = {}
    for i in range(20):
        asyncio.run(feed._poll_once(T0 + timedelta(seconds=i)))
    assert deaths == []
    assert "NEW" in feed.tracked()

    # once it prints a single price, the TIME-based dead timer arms
    jc.prices = {"NEW": 0.5}
    asyncio.run(feed._poll_once(T0 + timedelta(seconds=30)))
    jc.prices = {}
    asyncio.run(feed._poll_once(T0 + timedelta(seconds=31)))   # absent_since = 31
    asyncio.run(feed._poll_once(T0 + timedelta(seconds=40)))   # 9s >= 5s -> dead
    assert deaths == [("NEW", 0.5)]


def test_note_alive_resets_dead_timer():
    """A datapi cross-confirmation (note_alive) resets the absence timer, so a transient
    price-API gap cannot finalize a token the true-candle stream still sees (F24)."""
    deaths = []
    jc = FakeJupiter()
    feed = PriceFeed(jc, on_tick=lambda *a: None,
                     on_dead=lambda mint, price: deaths.append((mint, price)),
                     dead_after_s=5.0)
    feed.track("M")
    jc.prices = {"M": 1.0}
    asyncio.run(feed._poll_once(T0))
    jc.prices = {}
    asyncio.run(feed._poll_once(T0 + timedelta(seconds=2)))     # absent_since = 2
    feed.note_alive("M")                                        # datapi still sees it -> reset
    asyncio.run(feed._poll_once(T0 + timedelta(seconds=4)))     # absent_since = 4
    asyncio.run(feed._poll_once(T0 + timedelta(seconds=8)))     # 4s < 5s -> NOT dead
    assert deaths == []
    asyncio.run(feed._poll_once(T0 + timedelta(seconds=10)))    # 6s >= 5s -> dead
    assert deaths == [("M", 1.0)]


def test_outlier_tick_quarantined_until_confirmed():
    """A single spurious high tick (would fire TP1 and remove the -30% stop forever) is
    dropped until a 2nd poll confirms it; a normal tick after it is emitted (F28)."""
    ticks = []
    jc = FakeJupiter()
    feed = PriceFeed(jc, on_tick=lambda m, p, ts: ticks.append(p), on_dead=lambda *a: None,
                     outlier_up=6.0, outlier_down=0.15)
    feed.track("M")
    jc.prices = {"M": 1.0}
    asyncio.run(feed._poll_once(T0))
    assert ticks == [1.0]
    jc.prices = {"M": 10.0}                                     # spurious 10x -> quarantined
    asyncio.run(feed._poll_once(T0 + timedelta(seconds=1)))
    assert ticks == [1.0]
    jc.prices = {"M": 1.1}                                      # back to normal -> spike dropped
    asyncio.run(feed._poll_once(T0 + timedelta(seconds=2)))
    assert ticks == [1.0, 1.1]


def test_outlier_confirmed_move_is_emitted():
    """A real large move (two consecutive agreeing polls) is accepted after one poll delay."""
    ticks = []
    jc = FakeJupiter()
    feed = PriceFeed(jc, on_tick=lambda m, p, ts: ticks.append(p), on_dead=lambda *a: None,
                     outlier_up=6.0, outlier_down=0.15)
    feed.track("M")
    jc.prices = {"M": 1.0}
    asyncio.run(feed._poll_once(T0))
    jc.prices = {"M": 10.0}
    asyncio.run(feed._poll_once(T0 + timedelta(seconds=1)))     # quarantined
    assert ticks == [1.0]
    jc.prices = {"M": 10.5}                                     # confirms ~10x
    asyncio.run(feed._poll_once(T0 + timedelta(seconds=2)))
    assert ticks == [1.0, 10.5]


def test_normal_dip_and_stop_not_quarantined():
    """The -50% dip and -30% stop moves are within the guard band and pass immediately."""
    ticks = []
    jc = FakeJupiter()
    feed = PriceFeed(jc, on_tick=lambda m, p, ts: ticks.append(p), on_dead=lambda *a: None)
    feed.track("M")
    jc.prices = {"M": 100.0}; asyncio.run(feed._poll_once(T0))
    jc.prices = {"M": 49.0};  asyncio.run(feed._poll_once(T0 + timedelta(seconds=1)))  # -51%
    jc.prices = {"M": 34.0};  asyncio.run(feed._poll_once(T0 + timedelta(seconds=2)))  # -30%
    assert ticks == [100.0, 49.0, 34.0]


def test_last_ok_ts_tracks_feed_liveness_and_fail_counter():
    """last_ok_ts advances on a successful poll (even with no mints) and stalls on failure,
    so a genuine outage becomes visible; consecutive_fail climbs while the API is down (F33)."""
    class Boom:
        def __init__(self): self.fail = False; self.prices = {}
        def price(self, mints):
            if self.fail:
                raise RuntimeError("api down")
            return {m: self.prices[m] for m in mints if m in self.prices}
    jc = Boom()
    feed = PriceFeed(jc, on_tick=lambda *a: None, on_dead=lambda *a: None)
    asyncio.run(feed._poll_once(T0))                            # no mints -> loop alive
    assert feed.last_ok_ts == T0
    feed.track("M"); jc.prices = {"M": 1.0}
    asyncio.run(feed._poll_once(T0 + timedelta(seconds=1)))
    assert feed.last_ok_ts == T0 + timedelta(seconds=1)
    jc.fail = True
    asyncio.run(feed._poll_once(T0 + timedelta(seconds=2)))
    assert feed.last_ok_ts == T0 + timedelta(seconds=1)         # stalled -> outage visible
    assert feed.consecutive_fail == 1


def _tick_candle(price: float, sec: int) -> Candle:
    ts = T0 + timedelta(seconds=sec)
    return Candle(ts=ts, open=price, high=price, low=price, close=price, volume=0.0)


def test_one_tick_candles_drive_strategy_at_tick_latency():
    """1-tick candles (o=h=l=c) trigger dip entry and the stop on the very tick they occur."""
    tr = TailRider(cfg=TailRiderConfig())

    tr.on_candle(_tick_candle(100.0, 0))    # sig = first tick open = 100
    tr.on_candle(_tick_candle(100.0, 1))
    assert tr.state is PositionState.WATCHING

    # dip level = 0.5 * 100 = 50; tick 49 <= 50 -> ENTER at 50 * 1.01 = 50.5 on THIS tick
    tr.on_candle(_tick_candle(49.0, 2))
    assert tr.state is PositionState.ENTERED
    assert tr.entry == pytest.approx(50.0 * 1.01)

    # stop level = 0.7 * 50.5 = 35.35; tick 35 <= 35.35 -> STOPPED on THIS tick
    tr.on_candle(_tick_candle(35.0, 3))
    assert tr.state is PositionState.STOPPED
