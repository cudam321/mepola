"""Latency-honest fill simulator — the integrity core of the measurement harness.

The point of this module is to refuse to flatter ourselves. A naive backtest fills at
the signal price with no costs and counts only the tokens that survived; that produces a
beautiful, fake edge. Here we instead model what a *downstream subscriber* actually gets:

  1. LATENCY: you do not fill at the call price. You fill at the price ``latency_seconds``
     after the message (your real reaction + broadcast + land time).
  2. SLIPPAGE: microcap fills move the price against you on both the buy and the sell.
  3. FEES: venue fee (PumpSwap ~30bps; pump.fun bonding curve is dynamic) on both sides.
  4. MEV: sandwich/priority drag per round trip.
  5. FIXED GAS: priority fee + Jito tip are paid on BOTH the buy and the sell tx, in SOL —
     deliberately brutal on small tickets, because that is the truth.
  6. DEAD-TOKEN DENOMINATOR: if there is no exit liquidity at the horizon (the pool stopped
     trading — the token died), the position is UNSELLABLE and scored as a total loss, not
     silently dropped.

``net_multiple`` is "what fraction of your notional you walk away with", 1.0 = break-even,
2.0 = doubled, 0.0 = total loss. It can go slightly negative on dust trades when fixed gas
exceeds the position's residual value — that is real and intended.
"""

from __future__ import annotations

from bisect import bisect_right
from datetime import timedelta
from typing import Optional

from memebot.config import CostModel, horizon_to_seconds
from memebot.models import FillResult, PriceSeries, Signal

_TIMEFRAME_SECONDS = {"minute": 60, "hour": 3600, "day": 86400}


def _bar_seconds(series: PriceSeries) -> int:
    return _TIMEFRAME_SECONDS.get(series.timeframe, 60) * max(1, series.aggregate)


def _staleness_tolerance(series: PriceSeries, bar_seconds: int) -> float:
    """How far past the last candle a timestamp may sit before we call the token
    dead/illiquid. Adaptive to the *actual* candle spacing: free-tier OHLCV is often
    sparse, so a fixed 1-2 bar rule would wrongly declare a quiet-but-alive token dead.
    Uses max(2 bars, 3x median gap), capped at 30 minutes."""
    candles = series.candles
    if len(candles) >= 3:
        gaps = sorted((candles[i + 1].ts - candles[i].ts).total_seconds() for i in range(len(candles) - 1))
        median_gap = gaps[len(gaps) // 2]
    else:
        median_gap = bar_seconds
    return min(max(2 * bar_seconds, 3 * median_gap), 1800.0)


def price_at(series: PriceSeries, t, mode: str = "close") -> Optional[float]:
    """Reference price at time ``t`` from the covering candle.

    Returns ``None`` when ``t`` is before the first candle (no data yet) or well beyond
    the last candle (pool stopped trading -> illiquid/dead). ``mode`` selects which field
    of the covering bar to use: "close" (default), "open", "high", "low".
    """
    if series.empty:
        return None
    candles = series.candles
    ts_list = [c.ts for c in candles]
    dur = _bar_seconds(series)

    # Beyond available data (token likely went illiquid / stopped trading). Tolerance
    # adapts to the real candle spacing so sparse-but-alive tokens are not misread as dead.
    if t > candles[-1].ts + timedelta(seconds=_staleness_tolerance(series, dur)):
        return None
    i = bisect_right(ts_list, t) - 1
    if i < 0:
        return None  # before the first candle
    bar = candles[i]
    return {"open": bar.open, "high": bar.high, "low": bar.low}.get(mode, bar.close)


def simulate_fill(
    signal: Signal,
    series: PriceSeries,
    horizon: str,
    cost: CostModel,
    *,
    unsellable_is_total_loss: bool = True,
    price_mode: str = "close",
) -> Optional[FillResult]:
    """Simulate buying ``signal`` and exiting after ``horizon``.

    Returns ``None`` only when the ENTRY itself cannot be priced (the call predates
    available price data) — such signals are excluded from stats rather than counted as
    wins or losses. A token that cannot be EXITED (dead pool) is returned as a sellable=
    False total loss when ``unsellable_is_total_loss`` (the honest default).
    """
    t_entry = signal.posted_at + timedelta(seconds=cost.latency_seconds)
    t_exit = t_entry + timedelta(seconds=horizon_to_seconds(horizon))

    entry_ref = price_at(series, t_entry, price_mode)
    if entry_ref is None or entry_ref <= 0:
        return None  # cannot price entry -> exclude from the sample

    notional = cost.trade_size_sol
    gas_per_tx = cost.fixed_sol_cost_per_tx()
    s_in = cost.entry_slippage_bps / 1e4
    s_out = cost.exit_slippage_bps / 1e4
    fee = cost.pumpswap_fee_bps / 1e4   # per side
    mev = cost.mev_drag_bps / 1e4

    exit_ref = price_at(series, t_exit, price_mode)
    if exit_ref is None or exit_ref <= 0:
        if not unsellable_is_total_loss:
            return None
        # Never sold: only the buy-side gas was spent; the bag is worthless.
        net = -gas_per_tx / notional if notional > 0 else 0.0
        return FillResult(
            mint=signal.mint or "",
            horizon=horizon,
            entry_time=t_entry,
            entry_price=entry_ref * (1 + s_in),
            exit_time=t_exit,
            exit_price=0.0,
            gross_multiple=0.0,
            net_multiple=net,
            total_cost_fraction=1.0,
            sellable=False,
            notes=["no_exit_liquidity->total_loss"],
        )

    gross = exit_ref / entry_ref
    # Multiplicative execution efficiency over a round trip (buy fee + sell fee + both
    # slippages + MEV), then subtract fixed gas for the two transactions.
    eff = (1 - s_in) * (1 - fee) * (1 - s_out) * (1 - fee) * (1 - mev)
    fixed_frac = (2 * gas_per_tx) / notional if notional > 0 else 0.0
    net = eff * gross - fixed_frac
    # Round-trip drag expressed as the cost fraction at break-even (gross == 1).
    drag = (1 - eff) + fixed_frac

    return FillResult(
        mint=signal.mint or "",
        horizon=horizon,
        entry_time=t_entry,
        entry_price=entry_ref * (1 + s_in),
        exit_time=t_exit,
        exit_price=exit_ref * (1 - s_out),
        gross_multiple=gross,
        net_multiple=net,
        total_cost_fraction=drag,
        sellable=True,
        notes=[],
    )
