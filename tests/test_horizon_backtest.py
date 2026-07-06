"""Tests for the horizon backtest aggregator (deterministic, fake data client)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from memebot.config import CostModel, Settings
from memebot.models import Candle, Pool, PriceSeries, Signal, SignalSide
from memebot.backtest.horizon_backtest import run_backtest

T0 = datetime(2026, 6, 1, 0, 0, 0, tzinfo=timezone.utc)


def _rising_series(mint: str, minutes: int, start_price: float, end_price: float) -> PriceSeries:
    candles = []
    for i in range(minutes + 1):
        p = start_price + (end_price - start_price) * (i / minutes)
        candles.append(Candle(ts=T0 + timedelta(minutes=i), open=p, high=p, low=p, close=p, volume=1.0))
    return PriceSeries(mint=mint, pool=Pool("p", "t", mint, "SOL"),
                       timeframe="minute", aggregate=1, candles=candles)


class FakeClient:
    """ALIVEpump prices for 30 min then dies; DEADpump has no pool at all."""

    def get_price_series(self, mint, start, end):
        if mint == "ALIVEpump":
            return _rising_series(mint, 30, 100.0, 400.0)  # 4x over 30 min, then no data
        return PriceSeries(mint=mint, pool=None, timeframe="minute", aggregate=1, candles=[])


def _settings() -> Settings:
    cost = CostModel(latency_seconds=0.0, entry_slippage_bps=0.0, exit_slippage_bps=0.0,
                     pumpfun_fee_bps=0.0, pumpswap_fee_bps=0.0, priority_fee_sol=0.0,
                     jito_tip_sol=0.0, mev_drag_bps=0.0, trade_size_sol=1.0)
    return Settings(horizons=("5m", "1h"), cost=cost)


def _sig(mid, mint=None, side=SignalSide.BUY):
    return Signal(source_channel="chan", message_id=mid, posted_at=T0, raw_text="x",
                  side=side, mint=mint, parse_confidence=1.0 if mint else 0.0)


def test_full_denominator_and_priced_counts():
    signals = [
        _sig(1, "ALIVEpump"),
        _sig(2, "ALIVEpump"),
        _sig(3, "DEADpump"),                       # tradable but no price data -> unpriced
        _sig(4, None, SignalSide.UNKNOWN),         # not tradable
    ]
    rep = run_backtest(signals, _settings(), FakeClient(), bootstrap_n=200)

    assert rep.n_signals == 4
    assert rep.n_parsed == 3                        # three BUY+mint
    assert rep.n_priced == 2                        # only ALIVEpump priced

    by = {h.horizon: h for h in rep.horizons}
    # 5m: both ALIVE trades priced and winners (price rose).
    assert by["5m"].n_trades == 2
    assert by["5m"].n_winners == 2
    assert by["5m"].n_zeros == 0
    assert by["5m"].median_net_multiple > 1.0
    # 1h: token died at +30m, so exit is unsellable -> both are total losses.
    assert by["1h"].n_trades == 2
    assert by["1h"].n_zeros == 2
    assert by["1h"].n_winners == 0
    assert by["1h"].total_return < 0.0


def test_profit_factor_and_total_return_on_winners():
    rep = run_backtest([_sig(1, "ALIVEpump"), _sig(2, "ALIVEpump")], _settings(),
                       FakeClient(), horizons=["5m"], bootstrap_n=100)
    h = rep.horizons[0]
    # Equal-weight portfolio of two winners -> positive total return, PF > 1.
    assert h.total_return > 0.0
    assert h.profit_factor > 1.0
    assert h.max_net_multiple >= h.median_net_multiple
