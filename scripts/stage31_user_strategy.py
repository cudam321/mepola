#!/usr/bin/env python3
"""Stage 31 — the USER's exact strategy, implemented and simulated on fresh real data.

Rules (as specified):
  - entry: 0 latency, right when the signal fires (fill = entry candle open + 1% exec slip)
  - SL: -50% from each entry
  - TP ladder: TP1 at 3x sell 33% (recover initial); then sell 25% OF REMAINING at each next 2x step
    (6x,12x,24x,48x); after 5 TPs total, steps switch to 3x (144x,432x,...), still 25% of remaining.
    The 25%-of-remaining moonbag never fully exits via TP -> always a runner for the power law.
  - re-entry on momentum: after a STOP-OUT, re-buy when price recovers 3x from the exit price;
    never hold two positions in the same token at once; up to 5 re-entries.

Each ENTRY (initial + re-entries) is one trade (1 unit of capital). Reports full-denominator per-trade
EV, gate, per-token compounded result, ANSEM detail, and the win/loss decomposition.

    set -a && . ./.env && set +a && PYTHONPATH=src:scripts python3 scripts/stage31_user_strategy.py
"""
from __future__ import annotations
import sys
from datetime import timedelta
from pathlib import Path
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src")); sys.path.insert(0, str(ROOT / "scripts"))
from memebot.ingest.telegram_mcp import load_corpus_json, first_call_per_mint  # noqa: E402
from memebot.data.cache import CachedPriceClient  # noqa: E402
from memebot.data.jupiter import JupiterChartsClient  # noqa: E402
import stage14_untruncated as S  # noqa: E402

CAP = 50.0
TP_COST = 0.015
STOP_COST = 0.05     # stop fills ~ -50% level minus 5% (optimistic for a microcap gap)
MAX_REENTRY = 5


def manage(cands, entry):
    """Manage one position from `entry` price over `cands` (candles at/after entry).
    Returns (realized_multiple, exited_via_stop, exit_price, exit_index)."""
    remaining, proceeds = 1.0, 0.0
    stop = 0.5 * entry
    n_tp, level = 0, 3.0
    for idx, c in enumerate(cands):
        if remaining <= 1e-9:
            return proceeds / entry, False, last_px, idx
        if c.low <= stop:                                   # pessimistic: stop first
            proceeds += remaining * stop * (1 - STOP_COST)
            return proceeds / entry, True, stop, idx
        while remaining > 1e-9 and c.high >= level * entry:
            sell = min(0.33 if n_tp == 0 else 0.25 * remaining, remaining)
            proceeds += sell * level * entry * (1 - TP_COST)
            remaining -= sell
            n_tp += 1
            level = level * 2 if n_tp < 5 else level * 3    # 3,6,12,24,48, then 144,432,...
        last_px = c.close
    if remaining > 1e-9:
        proceeds += remaining * cands[-1].close
    return proceeds / entry, False, cands[-1].close, len(cands) - 1


def run_token(cands, f0):
    """Full lifecycle incl. re-entry after stop-outs. Returns list of per-entry trade multiples."""
    trades = []
    i = 0
    entry = f0
    reentries = 0
    while i < len(cands):
        seg = cands[i:]
        mult, stopped, exit_px, off = manage(seg, entry)
        trades.append(mult)
        if not stopped or reentries >= MAX_REENTRY:
            break
        # flat after a stop-out; watch for a 3x recovery from the exit price to re-enter
        j = i + off + 1
        target = 3.0 * exit_px
        while j < len(cands) and cands[j].high < target:
            j += 1
        if j >= len(cands):
            break
        entry = target * 1.01      # re-enter on the 3x momentum breakout
        i = j
        reentries += 1
    return trades


def main() -> int:
    calls = sorted([s for s in first_call_per_mint(load_corpus_json(str(ROOT / "runs" / "your_channel_fresh.json"))) if s.mint],
                   key=lambda s: s.posted_at)
    client = CachedPriceClient(JupiterChartsClient(min_interval=0.4), str(ROOT / "data_cache" / "jupiter_untrunc"))
    per_trade, trade_times, per_token, tok_times, ansem = [], [], [], [], None
    n_tok = 0
    for k, s in enumerate(calls):
        if k % 200 == 0:
            print(f"\r  {k}/{len(calls)}", end="", file=sys.stderr)
        t0 = s.posted_at
        try:
            ser = S.series_to_today(client, s.mint, t0)
        except Exception:
            ser = None
        if not ser or not ser.candles:
            continue
        cands = [c for c in ser.candles if c.ts >= t0]
        if not cands:
            continue
        f0 = cands[0].open * 1.01     # 0-latency instant fill + 1% exec slip
        if f0 <= 0:
            continue
        trades = run_token(cands, f0)
        if not trades:
            continue
        n_tok += 1
        for m in trades:
            per_trade.append(min(m, CAP)); trade_times.append(s.posted_at.timestamp())
        # per-token compounded (each entry recycles capital): product isn't right (parallel units);
        # use average capital return = mean of the entry multiples (each entry = 1 unit deployed)
        tok_result = float(np.mean([min(m, CAP) for m in trades]))
        per_token.append(tok_result); tok_times.append(s.posted_at.timestamp())
        if s.mint == "9cRCn9rGT8V2imeM2BaKs13yhMEais3ruM3rPvTGpump":
            ansem = trades
    print("\r" + " " * 30 + "\r", end="", file=sys.stderr)

    a = np.array(per_trade)
    mean, lo, hi = S.mean_ci(list(a)); d3 = S.drop_top(list(a), 3); g2 = S.fixed_f_growth(list(a), 0.02)
    bank = S.single_pass_bankroll(list(a), np.asarray(trade_times), 0.02, float("inf"))
    winr = (a > 1).mean() * 100
    print("=" * 100)
    print(f"  STAGE 31 — YOUR EXACT STRATEGY | {n_tok} tokens, {len(a)} total trades (incl re-entries) | cap {CAP}x")
    print("=" * 100)
    print(f"\n  PER-TRADE (each entry = 1 unit of capital):")
    print(f"    mean={mean:.3f}x  CIlo={lo:.3f}  drop3={d3:.3f}  f2logG={g2:+.4f}  win={winr:.0f}%  $500->{bank:.0f}")
    print(f"    VERDICT: {'*** GO ***' if (lo>1 and d3>1 and g2>0 and bank>500) else 'NO-GO'} (need CIlo>1, drop3>1, logG>0, $500 grows)")

    at = np.array(per_token)
    m2, l2, h2 = S.mean_ci(list(at))
    print(f"\n  PER-TOKEN (avg across its entries): mean={m2:.3f}x  CIlo={l2:.3f}  $500(1 tok each)->{S.single_pass_bankroll(list(at),np.asarray(tok_times),0.02,float('inf')):.0f}")

    wins = a[a > 1]; losses = a[a <= 1]
    print(f"\n  decomposition: {winr:.0f}% win (avg {wins.mean():.2f}x, n={len(wins)}) | {100-winr:.0f}% lose (avg {losses.mean():.2f}x, n={len(losses)})")
    print(f"    reached >=3x (got the ladder): {(a>=3).mean()*100:.1f}%  |  >=10x: {(a>=10).mean()*100:.1f}%  |  max {a.max():.0f}x")
    print(f"    total trades from re-entries: {len(a)-n_tok} extra ({(len(a)-n_tok)/n_tok*100:.0f}% more than one-per-token)")
    if ansem:
        print(f"\n  ANSEM entries (each is proceeds/entry): {[round(x,1) for x in ansem]}  -> avg {np.mean([min(x,CAP) for x in ansem]):.1f}x")
    print("=" * 100)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
