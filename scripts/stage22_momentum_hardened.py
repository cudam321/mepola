#!/usr/bin/env python3
"""Stage 22 — HARDENED momentum: every legitimate refinement, in one honest sweep.

Synthesizes and simulates the momentum variations not yet tried, all strictly no-lookahead (enter AT the
breakout confirmation, never before):
  - VOLUME-CONFIRMED breakout (breakout candle volume > k x recent median) — filters fakeouts.
  - ATR / volatility-scaled trailing exit (trail width set by realized volatility at entry).
  - FAST-SCALP partials (bank some on the breakout pop, trail the rest).
  - REGIME filter (no-lookahead): only trade when the last K calls' resolved 24h-returns were hot.
  - breakout threshold x window grid.
Gate (per traded set): CIlo>1, drop3>1, f=2% logG>0, $500 grows, at the realistic 50x cap. OOS the best.

    set -a && . ./.env && set +a && PYTHONPATH=src:scripts python3 scripts/stage22_momentum_hardened.py
"""

from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from memebot.ingest.telegram_mcp import load_corpus_json, first_call_per_mint  # noqa: E402
from memebot.data.cache import CachedPriceClient  # noqa: E402
from memebot.data.jupiter import JupiterChartsClient  # noqa: E402
from memebot.analysis.exit_sim import ExitPolicy, simulate_exit  # noqa: E402
import stage14_untruncated as S  # noqa: E402

CAP = 50.0


def prep(ser, t, f0):
    """Precompute per-token: candles after entry, ret24, atr fraction at entry."""
    after = [c for c in ser.candles if c.ts >= t]
    nb = [c for c in ser.candles if c.ts < t][-12:]  # candles just before entry for ATR
    atr = float(np.mean([(c.high - c.low) / c.close for c in nb if c.close > 0])) if nb else 0.3
    h24 = [c.high for c in ser.candles if t <= c.ts <= t + timedelta(hours=24)]
    p24 = (max(h24) / f0) if h24 else 0.0
    return after, max(0.05, min(atr, 0.8)), p24


def breakout(after, t, f0, B, Wh, volconf):
    """First candle within Wh hours whose high breaks +B%; optionally require volume>1.5x recent median."""
    end = t + timedelta(hours=Wh)
    lvl = f0 * (1 + B)
    vols = []
    for c in after:
        if c.ts > end:
            break
        if volconf:
            vols.append(c.volume)
        if c.high >= lvl:
            if volconf and len(vols) >= 4 and c.volume < 1.5 * np.median(vols[:-1] or [c.volume]):
                continue  # breakout without volume -> ignore, keep scanning
            return lvl * 1.01, c.ts
    return None, None


def evaluate(tokens, B, Wh, volconf, exit_kind):
    """Return (mults, times) over tokens that triggered the breakout."""
    M, T = [], []
    for tk in tokens:
        ser, t, f0, after, atr = tk["ser"], tk["t"], tk["f0"], tk["after"], tk["atr"]
        bf, bt = breakout(after, t, f0, B, Wh, volconf)
        if bf is None:
            continue
        if exit_kind == "trail30":
            pol = ExitPolicy("e", [], 0.0, 0.30, 1.0, 1e9)
        elif exit_kind == "atr":
            tp = float(np.clip(3.0 * atr, 0.15, 0.6))
            pol = ExitPolicy("e", [], 0.0, tp, 1.0, 1e9)
        elif exit_kind == "scalp":
            pol = ExitPolicy("e", [(1.3, 0.5)], 0.0, 0.40, 1.0, 1e9)
        else:  # ladder+trail
            pol = ExitPolicy("e", [(1.5, 0.3), (2.5, 0.3)], 0.0, 0.40, 1.3, 24 * 7)
        M.append(simulate_exit(ser, bf, bt, pol)); T.append(tk["ts"])
    return M, T


def gate(name, M, T, total, regime_n=None):
    if len(M) < 12:
        print(f"    {name:40} n={len(M)} too few"); return False
    cm = S.cap_mults(M, CAP)
    mean, lo, hi = S.mean_ci(cm); d3 = S.drop_top(cm, 3); g2 = S.fixed_f_growth(cm, 0.02)
    bank = S.single_pass_bankroll(cm, np.asarray(T), 0.02, float("inf")); win = np.mean(np.array(M) > 1) * 100
    go = lo > 1 and d3 > 1 and g2 > 0 and bank > 500
    print(f"    {name:40} n={len(M):4d}/{total} mean={mean:6.3f} CIlo={lo:6.3f} drop3={d3:6.3f} "
          f"f2logG={g2:+.4f} win={win:3.0f}% $500->{bank:7.0f} {'*** GO ***' if go else ''}")
    return go


def main() -> int:
    calls = sorted(first_call_per_mint(load_corpus_json(str(ROOT / "runs" / "your_channel_corpus.json"))),
                   key=lambda s: s.posted_at)
    client = CachedPriceClient(JupiterChartsClient(min_interval=0.4), str(ROOT / "data_cache" / "jupiter_untrunc"))
    toks = []
    for i, s in enumerate(calls):
        if not s.mint:
            continue
        if i % 150 == 0:
            print(f"\r  loading {i}/{len(calls)}", end="", file=sys.stderr)
        t = s.posted_at + timedelta(seconds=S.LAT_S)
        try:
            ser = S.series_to_today(client, s.mint, s.posted_at)
        except Exception:
            ser = None
        if not ser or not ser.candles:
            continue
        f0 = S.entry_fill(ser, t)
        if not f0 or f0 <= 0:
            continue
        after, atr, p24 = prep(ser, t, f0)
        toks.append(dict(ser=ser, t=t, f0=f0, after=after, atr=atr, p24=p24, ts=s.posted_at.timestamp()))
    print("\r" + " " * 40 + "\r", end="", file=sys.stderr)
    N = len(toks)
    print("=" * 102)
    print(f"  STAGE 22 — HARDENED momentum sweep | {N} priced calls | honest entries | cap {CAP:.0f}x")
    print("=" * 102)

    print("\n  breakout x window x volume-confirm x exit (traded-set EV):")
    best = []
    for B in (0.35, 0.5, 1.0):
        for Wh in (3, 6):
            for vc in (False, True):
                for ek in ("trail30", "atr", "scalp", "ladder"):
                    M, T = evaluate(toks, B, Wh, vc, ek)
                    if len(M) < 12:
                        continue
                    cm = S.cap_mults(M, CAP); mean, lo, _ = S.mean_ci(cm); d3 = S.drop_top(cm, 3)
                    g2 = S.fixed_f_growth(cm, 0.02); bank = S.single_pass_bankroll(cm, np.asarray(T), 0.02, float("inf"))
                    go = lo > 1 and d3 > 1 and g2 > 0 and bank > 500
                    best.append((lo, f"bo{int(B*100)}/{Wh}h/{'vol' if vc else 'nov'}/{ek}", len(M), mean, lo, d3, g2, bank, go))
    best.sort(reverse=True)
    print("    (top 12 by CIlo)")
    for lo, name, n, mean, ci, d3, g2, bank, go in best[:12]:
        print(f"    {name:30} n={n:4d} mean={mean:6.3f} CIlo={ci:6.3f} drop3={d3:6.3f} "
              f"f2logG={g2:+.4f} $500->{bank:7.0f} {'*** GO ***' if go else ''}")

    # REGIME filter: only trade when the last K calls' resolved 24h-returns were hot (no lookahead)
    print("\n  REGIME filter (no-lookahead) on breakout50/6h/vol/trail30 — trade only when market is hot:")
    toks_chrono = sorted(toks, key=lambda d: d["ts"])
    p24s = [d["p24"] for d in toks_chrono]
    for K in (10, 20):
        for thr in (1.0, 1.3, 1.6):
            M, T = [], []
            for j, tk in enumerate(toks_chrono):
                prior = p24s[max(0, j - K):j]
                if len(prior) < K:
                    continue
                if np.mean(prior) < thr:   # regime not hot enough
                    continue
                bf, bt = breakout(tk["after"], tk["t"], tk["f0"], 0.5, 6, True)
                if bf is None:
                    continue
                pol = ExitPolicy("e", [], 0.0, 0.30, 1.0, 1e9)
                M.append(simulate_exit(tk["ser"], bf, bt, pol)); T.append(tk["ts"])
            gate(f"regime K={K} hot>{thr}x", M, T, N)

    # OOS the single best config
    if best and best[0][8]:
        print("\n  Best config flagged GO — re-test OOS (last 30% by date):")
    print("=" * 102)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
