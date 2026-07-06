"""Phase 0.5 engine: turn a corpus of calls into a PnL-by-holding-horizon table.

This is the cheapest falsification of the channel's edge. For every BUY signal with a
resolvable mint, we fetch the post-call price path and simulate an honest fill at each
exit horizon, *including* the calls whose tokens went to zero. The output answers the one
pivotal question: at which holding horizon (if any) does this channel show a real,
latency- and cost-survivable edge?

Read the result against the plan's Go/No-Go gates: profit factor >= ~1.3 on NET multiples
(not win rate), a believable 45-60% win rate, and PnL carried by held right-tail winners.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Callable, Optional, Protocol

import numpy as np

from memebot.config import Settings, horizon_to_seconds
from memebot.models import (
    BacktestReport,
    FillResult,
    HorizonResult,
    PriceSeries,
    Signal,
)
from memebot.sim.fill_simulator import simulate_fill

_ENTRY_BUFFER = timedelta(minutes=2)
_EXIT_BUFFER = timedelta(minutes=5)


class DataClient(Protocol):
    def get_price_series(self, mint: str, start: datetime, end: datetime) -> PriceSeries: ...


def _aggregate(horizon: str, fills: list[FillResult], *, bootstrap_n: int, rng: np.random.Generator) -> HorizonResult:
    nets = np.array([f.net_multiple for f in fills], dtype=float)
    n = len(nets)
    if n == 0:
        return HorizonResult(horizon, 0, 0, 0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, (0.0, 0.0))

    winners = int(np.sum(nets > 1.0))
    zeros = int(sum(1 for f in fills if (not f.sellable) or f.net_multiple <= 0.05))
    gross_profit = float(np.sum(np.clip(nets - 1.0, 0, None)))
    gross_loss = float(np.sum(np.clip(1.0 - nets, 0, None)))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    # Equal-weight portfolio: invest one unit per call; portfolio multiple = mean(net).
    mean_net = float(np.mean(nets))
    total_return = mean_net - 1.0

    # Bootstrap a 95% CI on the equal-weight total return (resample calls with replacement).
    if n >= 2 and bootstrap_n > 0:
        idx = rng.integers(0, n, size=(bootstrap_n, n))
        boot_means = nets[idx].mean(axis=1) - 1.0
        ci = (float(np.percentile(boot_means, 2.5)), float(np.percentile(boot_means, 97.5)))
    else:
        ci = (total_return, total_return)

    return HorizonResult(
        horizon=horizon,
        n_trades=n,
        n_winners=winners,
        n_zeros=zeros,
        win_rate=winners / n,
        mean_net_multiple=mean_net,
        median_net_multiple=float(np.median(nets)),
        profit_factor=profit_factor,
        total_return=total_return,
        p25_net_multiple=float(np.percentile(nets, 25)),
        p75_net_multiple=float(np.percentile(nets, 75)),
        max_net_multiple=float(np.max(nets)),
        ci95_total_return=ci,
    )


def run_backtest(
    signals: list[Signal],
    settings: Settings,
    data_client: DataClient,
    *,
    horizons: Optional[list[str]] = None,
    price_mode: str = "close",
    bootstrap_n: int = 2000,
    seed: int = 7,
    progress: Optional[Callable[[int, int, Signal], None]] = None,
) -> BacktestReport:
    horizons = list(horizons or settings.horizons)
    max_secs = max(horizon_to_seconds(h) for h in horizons)
    rng = np.random.default_rng(seed)

    tradable = [s for s in signals if s.is_tradable]
    by_horizon: dict[str, list[FillResult]] = {h: [] for h in horizons}
    n_priced = 0

    for i, sig in enumerate(tradable):
        if progress:
            progress(i, len(tradable), sig)
        t_entry = sig.posted_at + timedelta(seconds=settings.cost.latency_seconds)
        start = t_entry - _ENTRY_BUFFER
        end = t_entry + timedelta(seconds=max_secs) + _EXIT_BUFFER
        try:
            series = data_client.get_price_series(sig.mint, start, end)
        except Exception:
            series = PriceSeries(mint=sig.mint or "", pool=None, timeframe="minute", aggregate=1, candles=[])
        if series.empty:
            continue
        priced_any = False
        for h in horizons:
            fr = simulate_fill(
                sig, series, h, settings.cost,
                unsellable_is_total_loss=settings.unsellable_is_total_loss,
                price_mode=price_mode,
            )
            if fr is None:
                continue
            priced_any = True
            by_horizon[h].append(fr)
        if priced_any:
            n_priced += 1

    horizon_results = [
        _aggregate(h, by_horizon[h], bootstrap_n=bootstrap_n, rng=rng) for h in horizons
    ]

    channel = tradable[0].source_channel if tradable else (signals[0].source_channel if signals else "unknown")
    cm = settings.cost
    return BacktestReport(
        channel=channel,
        generated_at=datetime.now(timezone.utc),
        n_signals=len(signals),
        n_parsed=len(tradable),
        n_priced=n_priced,
        horizons=horizon_results,
        cost_assumptions={
            "latency_seconds": cm.latency_seconds,
            "entry_slippage_bps": cm.entry_slippage_bps,
            "exit_slippage_bps": cm.exit_slippage_bps,
            "pumpswap_fee_bps": cm.pumpswap_fee_bps,
            "mev_drag_bps": cm.mev_drag_bps,
            "priority_fee_sol": cm.priority_fee_sol,
            "jito_tip_sol": cm.jito_tip_sol,
            "trade_size_sol": cm.trade_size_sol,
            "price_mode": price_mode,
        },
        notes=[
            "net_multiple includes latency, slippage, venue fee, MEV drag, and fixed gas on both txs.",
            "Tokens with no exit liquidity at the horizon are scored as total losses (full denominator).",
            "Profit factor and total return are on NET multiples; judge edge by these, not win rate.",
        ],
    )


def _fmt_pf(pf: float) -> str:
    return "inf" if pf == float("inf") else f"{pf:.2f}"


def format_report(report: BacktestReport) -> str:
    cm = report.cost_assumptions
    lines: list[str] = []
    lines.append("=" * 92)
    lines.append(f"  CHANNEL EDGE BACKTEST — {report.channel}")
    lines.append(f"  generated {report.generated_at.isoformat()}")
    lines.append("=" * 92)
    lines.append(
        f"  signals={report.n_signals}  tradable(buy+mint)={report.n_parsed}  "
        f"priced={report.n_priced}"
    )
    lines.append(
        "  costs: latency={ls}s  slip={si}/{so}bps  fee={f}bps  mev={m}bps  "
        "gas={pf}+{tip}SOL/tx  size={sz}SOL  px={px}".format(
            ls=cm.get("latency_seconds"), si=cm.get("entry_slippage_bps"),
            so=cm.get("exit_slippage_bps"), f=cm.get("pumpswap_fee_bps"),
            m=cm.get("mev_drag_bps"), pf=cm.get("priority_fee_sol"),
            tip=cm.get("jito_tip_sol"), sz=cm.get("trade_size_sol"), px=cm.get("price_mode"),
        )
    )
    lines.append("-" * 92)
    header = f"  {'horizon':>7} | {'n':>4} | {'win%':>5} | {'med×':>6} | {'mean×':>7} | {'PF':>5} | {'totRet%':>8} | {'CI95 totRet%':>20} | {'max×':>8}"
    lines.append(header)
    lines.append("-" * 92)
    for h in report.horizons:
        ci = h.ci95_total_return
        lines.append(
            f"  {h.horizon:>7} | {h.n_trades:>4} | {h.win_rate*100:>4.0f}% | "
            f"{h.median_net_multiple:>6.2f} | {h.mean_net_multiple:>7.2f} | {_fmt_pf(h.profit_factor):>5} | "
            f"{h.total_return*100:>7.1f}% | [{ci[0]*100:>7.1f}%, {ci[1]*100:>7.1f}%] | {h.max_net_multiple:>8.1f}"
        )
    lines.append("-" * 92)

    # Gate read-out: best horizon by profit factor, with the honest verdict.
    priced = [h for h in report.horizons if h.n_trades > 0]
    if priced:
        best = max(priced, key=lambda h: (h.profit_factor if h.profit_factor != float("inf") else 1e9))
        pf = best.profit_factor
        ci_low = best.ci95_total_return[0]
        verdict = (
            "EDGE PLAUSIBLE — proceed to forward OOS validation (Phase 3)."
            if (pf >= 1.3 and ci_low > 0)
            else "NO CREDIBLE EDGE under honest costs — do NOT risk capital; refine latency/horizon or stop."
        )
        lines.append(f"  best horizon by PF: {best.horizon}  (PF={_fmt_pf(pf)}, totRet 95%CI low={ci_low*100:.1f}%)")
        lines.append(f"  VERDICT: {verdict}")
    else:
        lines.append("  VERDICT: no priced trades — check mint resolution / data coverage / timestamps.")
    lines.append("=" * 92)
    lines.append("  Reminder: win rate is a vanity metric here. Judge by NET profit factor, a")
    lines.append("  positive CI lower bound, and whether PnL is carried by HELD right-tail winners.")
    lines.append("=" * 92)
    return "\n".join(lines)
