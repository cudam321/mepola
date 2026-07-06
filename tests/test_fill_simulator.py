"""Tests for the latency-honest fill simulator (deterministic, no network)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from memebot.config import CostModel
from memebot.models import Candle, Pool, PriceSeries, Signal, SignalSide
from memebot.sim.fill_simulator import price_at, simulate_fill

T0 = datetime(2026, 6, 1, 0, 0, 0, tzinfo=timezone.utc)


def _flat_then_double() -> PriceSeries:
    """6 one-minute candles: price 100 at 00:00, doubling to 200 by 00:05."""
    prices = [100, 120, 140, 160, 180, 200]
    candles = [
        Candle(ts=T0 + timedelta(minutes=i), open=p, high=p, low=p, close=p, volume=1000.0)
        for i, p in enumerate(prices)
    ]
    return PriceSeries(mint="TESTpump", pool=Pool("pool", "test", "TESTpump", "SOL"),
                       timeframe="minute", aggregate=1, candles=candles)


def _signal(side=SignalSide.BUY, mint="TESTpump", posted=T0) -> Signal:
    return Signal(source_channel="t", message_id=1, posted_at=posted, raw_text="buy",
                  side=side, mint=mint, parse_confidence=1.0)


def _zero_cost(**over) -> CostModel:
    base = dict(latency_seconds=0.0, entry_slippage_bps=0.0, exit_slippage_bps=0.0,
                pumpfun_fee_bps=0.0, pumpswap_fee_bps=0.0, priority_fee_sol=0.0,
                jito_tip_sol=0.0, mev_drag_bps=0.0, trade_size_sol=1.0)
    base.update(over)
    return CostModel(**base)


def test_price_at_covering_bar():
    s = _flat_then_double()
    assert price_at(s, T0) == 100
    assert price_at(s, T0 + timedelta(minutes=5)) == 200
    # before the series -> None
    assert price_at(s, T0 - timedelta(minutes=1)) is None
    # well beyond the series -> None (illiquid)
    assert price_at(s, T0 + timedelta(hours=2)) is None


def test_zero_cost_recovers_gross():
    s = _flat_then_double()
    fr = simulate_fill(_signal(), s, "5m", _zero_cost())
    assert fr is not None and fr.sellable
    assert fr.gross_multiple == 2.0
    assert abs(fr.net_multiple - 2.0) < 1e-9  # no costs -> net == gross


def test_costs_reduce_net_below_gross():
    s = _flat_then_double()
    cheap = simulate_fill(_signal(), s, "5m", _zero_cost())
    pricey = simulate_fill(_signal(), s, "5m",
                           _zero_cost(entry_slippage_bps=300, exit_slippage_bps=300,
                                      pumpswap_fee_bps=30, mev_drag_bps=70))
    assert pricey.net_multiple < cheap.net_multiple < 2.0 + 1e-9
    assert pricey.net_multiple > 1.0  # a real 2x still survives these costs


def test_fixed_gas_dominates_dust():
    """On a tiny ticket, fixed SOL gas (tip+priority, both txs) is a heavy drag."""
    s = _flat_then_double()
    dust = simulate_fill(_signal(), s, "5m",
                         _zero_cost(priority_fee_sol=0.001, jito_tip_sol=0.005, trade_size_sol=0.1))
    big = simulate_fill(_signal(), s, "5m",
                        _zero_cost(priority_fee_sol=0.001, jito_tip_sol=0.005, trade_size_sol=10.0))
    # 2*(0.006)/0.1 = 0.12 multiple drag on dust vs negligible on the big ticket.
    assert big.net_multiple - dust.net_multiple > 0.10


def test_unsellable_is_total_loss():
    s = _flat_then_double()
    fr = simulate_fill(_signal(), s, "1h", _zero_cost(jito_tip_sol=0.005, trade_size_sol=0.1))
    assert fr is not None
    assert fr.sellable is False
    assert fr.gross_multiple == 0.0
    assert fr.net_multiple <= 0.0  # bag worthless; only buy gas spent
    assert "no_exit_liquidity->total_loss" in fr.notes


def test_unpriced_entry_returns_none():
    s = _flat_then_double()
    # Call posted before any price data -> entry cannot be priced -> excluded.
    fr = simulate_fill(_signal(posted=T0 - timedelta(minutes=10)), s, "5m", _zero_cost())
    assert fr is None
