#!/usr/bin/env python3
"""REFUTE part 3: anchor validity via data sparsity + concentration of the fairest fill.

Even the 30min-MEDIAN fill kept the GO. Test whether THAT result is itself carried by
(i) thin/illiquid early candles (anchor lands before real liquidity -> stale launch-low fill)
and (ii) a handful of tokens. For each token measure the GAP between the intended fill time
te and the actual nearest candle, and the candle count near te. Then re-gate the 30min-median
early P_MOON on liquidity-clean subsets and report concentration.
"""
from __future__ import annotations
import sys
from datetime import timedelta
from pathlib import Path
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from memebot.ingest.telegram_mcp import load_corpus_json, first_call_per_mint
from memebot.analysis.features import extract_features
from memebot.data.cache import CachedPriceClient
from memebot.data.jupiter import JupiterChartsClient
from memebot.analysis.exit_sim import simulate_exit
import stage14_untruncated as S

MAX_TSE_H = 72.0


def gate(idx, mults, times, label):
    cm = S.cap_mults([mults[i] for i in idx], 50.0)
    tt = np.asarray([times[i] for i in idx])
    m, lo, hi = S.mean_ci(cm)
    d3 = S.drop_top(cm, 3); d5 = S.drop_top(cm, 5); d10 = S.drop_top(cm, 10)
    g2 = S.fixed_f_growth(cm, 0.02)
    bank = S.single_pass_bankroll(cm, tt, 0.02, float("inf"))
    go = lo > 1 and d3 > 1 and g2 > 0 and bank > 500
    print(f"  {label:44} n={len(cm):3d} mean {m:6.3f} CIlo {lo:6.3f} drop3 {d3:6.3f} "
          f"drop5 {d5:6.3f} drop10 {d10:6.3f} $500->{bank:8.0f}  {'*** GO ***' if go else 'NO-GO'}")
    return go


def main():
    calls = sorted(first_call_per_mint(load_corpus_json(str(ROOT / "runs" / "your_channel_corpus.json"))),
                   key=lambda s: s.posted_at)
    client = CachedPriceClient(JupiterChartsClient(min_interval=0.4), str(ROOT / "data_cache" / "jupiter_earlyseat"))

    rows = []
    for s in calls:
        if not s.mint:
            continue
        f = extract_features(s.raw_text)
        tse = f["time_since_entry_h"]
        if tse is None or not (0.0 <= tse <= MAX_TSE_H):
            continue
        t_smart = s.posted_at - timedelta(hours=tse)
        te = t_smart + timedelta(seconds=S.LAT_S)
        try:
            ser_e = S.series_to_today(client, s.mint, t_smart)
            ser_l = S.series_to_today(client, s.mint, s.posted_at)
        except Exception:
            ser_e = ser_l = None
        if not (ser_e and ser_e.candles):
            continue
        fe_90s = S.entry_fill(ser_e, te)
        fl = S.entry_fill(ser_l, s.posted_at + timedelta(seconds=S.LAT_S)) if (ser_l and ser_l.candles) else None
        if fe_90s is None or fe_90s <= 0 or fl is None or fl <= 0:
            continue

        # nearest candle to te + gap; candle count within +-30min of te
        cl = ser_e.candles
        nearest = min(cl, key=lambda c: abs((c.ts - te).total_seconds()))
        gap_min = abs((nearest.ts - te).total_seconds()) / 60.0
        near30 = sum(1 for c in cl if abs((c.ts - te).total_seconds()) <= 30 * 60)
        # is te BEFORE the first candle (anchor predates liquidity -> stale)?
        first_ts = cl[0].ts
        te_before_first_min = max(0.0, (first_ts - te).total_seconds() / 60.0)

        w30 = [c.high for c in cl if te <= c.ts <= te + timedelta(minutes=30)]
        fe_30m_med = float(np.median(w30)) * 1.015 if w30 else fe_90s

        rows.append(dict(
            mint=s.mint, tse=tse, gap_min=gap_min, near30=near30,
            te_before_first_min=te_before_first_min,
            moon_30m_med=simulate_exit(ser_e, fe_30m_med, te, S.P_MOON),
            t=s.posted_at.timestamp(),
        ))

    n = len(rows)
    moon = [r["moon_30m_med"] for r in rows]
    times = [r["t"] for r in rows]
    gap = np.array([r["gap_min"] for r in rows])
    near30 = np.array([r["near30"] for r in rows])
    tbf = np.array([r["te_before_first_min"] for r in rows])

    print(f"\n=== ANCHOR-VALIDITY / SPARSITY on the FAIREST fill (30min-median) | n={n} ===\n")
    print(f"  gap te->nearest candle (min): median={np.median(gap):.1f} mean={gap.mean():.1f} "
          f"p90={np.percentile(gap,90):.0f} p99={np.percentile(gap,99):.0f} max={gap.max():.0f}")
    print(f"  share with gap>5min: {(gap>5).mean()*100:.0f}%   >30min: {(gap>30).mean()*100:.0f}%   "
          f">120min: {(gap>120).mean()*100:.0f}%")
    print(f"  candles within +-30min of te: median={np.median(near30):.0f} "
          f"share with <=2 (thin): {(near30<=2).mean()*100:.0f}%")
    print(f"  anchor te BEFORE first available candle (stale launch fill): "
          f"{(tbf>0).mean()*100:.0f}% of tokens; of those median lead={np.median(tbf[tbf>0]) if (tbf>0).any() else 0:.0f}min\n")

    gate(range(n), moon, times, "ALL (30min-median fill)")
    gate([i for i in range(n) if gap[i] <= 5], moon, times, "gap<=5min (fill near intended anchor)")
    gate([i for i in range(n) if near30[i] >= 5], moon, times, "candles>=5 near entry (liquid)")
    gate([i for i in range(n) if tbf[i] == 0], moon, times, "anchor NOT before first candle")
    gate([i for i in range(n) if gap[i] <= 5 and near30[i] >= 5], moon, times, "liquid AND on-anchor")

    # concentration of the 30min-median GO
    a = np.array(S.cap_mults(moon, 50.0))
    order = np.argsort(a)[::-1]
    print(f"\n  concentration (30min-median, 50x cap): top-1 token moon={a[order[0]]:.1f} "
          f"({rows[order[0]]['mint'][:8]}, near30={rows[order[0]]['near30']}, gap={rows[order[0]]['gap_min']:.1f}m)")
    print(f"  sum(top3)/sum = {a[order[:3]].sum()/a.sum()*100:.0f}%   "
          f"top5={a[order[:5]].sum()/a.sum()*100:.0f}%   top10={a[order[:10]].sum()/a.sum()*100:.0f}%")
    print(f"  the GO's mean-1 'edge' = {a.mean()-1:.3f}; contributed by top-5 tokens: "
          f"{(a[order[:5]].sum()-5)/(a.sum()-n)*100:.0f}% of total excess")


if __name__ == "__main__":
    main()
