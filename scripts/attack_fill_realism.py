#!/usr/bin/env python3
"""ATTACK on stage19 early-seat GO: fill realism at thin early liquidity.

The early seat enters at smart money's OWN entry moment t_smart, where the token is
young/small -> THIN liquidity. stage19's fill = max-high-in-90s * 1.015 (a 1.5% slip).
That is wildly optimistic for early memecoin liquidity, which routinely costs 10-40% to
enter size. We re-price the SAME 168 tse<=72h tokens with progressively worse early fills
and re-run the path-dependent P_MOON simulation, then re-aggregate the GATE metrics.

Slip is applied to the BASE 90s max-high price (we strip the baseline 1.015 and re-apply):
  base = max-high in [t_e, t_e+90s]
  fe(s) = base * (1 + s),   s in {0.015 (baseline), 0.10, 0.25, 0.50}
Plus two "chasing" fills:
  HIGHx1.05      : entry candle's high * 1.05
  next_cand_high : chase into the next candle, fill at its high (entry+sim shift forward)

For each: P_MOON path-dependent re-sim -> mean / bootstrap CIlo / drop-top3 / f=2% logG /
$500 single-pass bankroll, at the realistic 50x cap (and 25x). GO needs CIlo>1 AND drop3>1.

    set -a && . ./.env && set +a && PYTHONPATH=src:scripts python3 scripts/attack_fill_realism.py
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
from memebot.analysis.features import extract_features  # noqa: E402
from memebot.data.cache import CachedPriceClient  # noqa: E402
from memebot.data.jupiter import JupiterChartsClient  # noqa: E402
from memebot.analysis.exit_sim import simulate_exit  # noqa: E402
import stage14_untruncated as S  # noqa: E402

MAX_TSE_H = 72.0
CAP = 50.0


def base_maxhigh_90s(series, t):
    """The 90s-window max-high BASE price (no slip), mirroring entry_fill before *1.015."""
    win = [c for c in series.candles if t <= c.ts <= t + timedelta(seconds=90)]
    if win:
        return max(c.high for c in win)
    prior = [c for c in series.candles if c.ts <= t]
    return prior[-1].high if prior else None


def entry_candle_high(series, t):
    """High of the candle covering the entry instant (last candle at/before t, else first >= t)."""
    prior = [c for c in series.candles if c.ts <= t]
    if prior:
        return prior[-1].high
    after = [c for c in series.candles if c.ts > t]
    return after[0].high if after else None


def next_candle(series, t):
    """The first candle strictly after the entry candle (chasing one bar later)."""
    after = [c for c in series.candles if c.ts > t]
    return after[0] if after else None


def agg(mults, times, caps=(25.0, 50.0)):
    out = {}
    for cap in caps:
        cm = S.cap_mults(mults, cap)
        m, lo, hi = S.mean_ci(cm)
        d3 = S.drop_top(cm, 3)
        g2 = S.fixed_f_growth(cm, 0.02)
        bank = S.single_pass_bankroll(cm, times, 0.02, float("inf"))
        go = lo > 1 and d3 > 1 and g2 > 0 and bank > 500
        out[f"{cap:.0f}x"] = dict(mean=m, ci_lo=lo, drop3=d3, f2logG=g2, bank=bank, go=go)
    return out


def main() -> int:
    calls = sorted(first_call_per_mint(load_corpus_json(str(ROOT / "runs" / "your_channel_corpus.json"))),
                   key=lambda s: s.posted_at)
    client = CachedPriceClient(JupiterChartsClient(min_interval=0.4),
                               str(ROOT / "data_cache" / "jupiter_earlyseat"))

    # Replicate stage19's exact 168-token selection and stash what we need per token.
    toks = []  # dict(ser_e, t_e, base, fe_orig, fl)
    n_anchored = 0
    for s in calls:
        if not s.mint:
            continue
        f = extract_features(s.raw_text)
        tse = f["time_since_entry_h"]
        if tse is None or not (0.0 <= tse <= MAX_TSE_H):
            continue
        n_anchored += 1
        if n_anchored % 25 == 0:
            print(f"\r  pricing {n_anchored} (cache {client.hits}h/{client.misses}m)", end="", file=sys.stderr)
        t_smart = s.posted_at - timedelta(hours=tse)
        t_post = s.posted_at + timedelta(seconds=S.LAT_S)
        t_e = t_smart + timedelta(seconds=S.LAT_S)
        try:
            ser_e = S.series_to_today(client, s.mint, t_smart)
            ser_l = S.series_to_today(client, s.mint, s.posted_at)
        except Exception:
            ser_e = ser_l = None
        fe = S.entry_fill(ser_e, t_e) if (ser_e and ser_e.candles) else None
        fl = S.entry_fill(ser_l, t_post) if (ser_l and ser_l.candles) else None
        if fe is None or fe <= 0 or fl is None or fl <= 0:
            continue
        base = base_maxhigh_90s(ser_e, t_e)
        toks.append(dict(ser_e=ser_e, t_e=t_e, base=base, fe_orig=fe, ts=s.posted_at.timestamp()))
    print("\r" + " " * 60 + "\r", end="", file=sys.stderr)

    n = len(toks)
    times = np.array([t["ts"] for t in toks])
    print("=" * 100)
    print(f"  ATTACK: fill realism at thin early liquidity | n={n} tokens (tse<=72h) | P_MOON | cap-sweep")
    print("=" * 100)

    # sanity: reconstructed base*1.015 should equal stored fe_orig
    recon_err = max(abs(t["base"] * 1.015 - t["fe_orig"]) / t["fe_orig"] for t in toks)
    print(f"  base-reconstruction max rel-err vs stored fe: {recon_err:.2e}  (should be ~0)\n")

    scenarios = []
    # slip scenarios on the 90s-max-high base
    for s in (0.015, 0.10, 0.25, 0.50):
        label = f"slip +{s*100:.1f}%"
        mults = []
        for t in toks:
            fe = t["base"] * (1 + s)
            mults.append(simulate_exit(t["ser_e"], fe, t["t_e"], S.P_MOON))
        scenarios.append((label, mults))

    # entry-candle HIGH * 1.05
    mults = []
    for t in toks:
        h = entry_candle_high(t["ser_e"], t["t_e"])
        fe = h * 1.05 if h else t["base"] * 1.05
        mults.append(simulate_exit(t["ser_e"], fe, t["t_e"], S.P_MOON))
    scenarios.append(("entryHIGH*1.05", mults))

    # next-candle high (chasing): entry & sim window shift to the next candle
    mults = []
    n_nochase = 0
    for t in toks:
        nc = next_candle(t["ser_e"], t["t_e"])
        if nc is None:
            n_nochase += 1
            mults.append(simulate_exit(t["ser_e"], t["fe_orig"], t["t_e"], S.P_MOON))
            continue
        fe = nc.high  # buy the next bar's high (pure chase, no extra slip)
        mults.append(simulate_exit(t["ser_e"], fe, nc.ts, S.P_MOON))
    scenarios.append((f"nextcand_high (chase; {n_nochase} fallbacks)", mults))

    print(f"  {'scenario':<30} {'cap':>5} | {'mean':>7} {'CIlo':>7} {'drop3':>7} {'f2logG':>9} {'$500':>10}  GO")
    print("  " + "-" * 96)
    results = {}
    for label, mults in scenarios:
        r = agg(mults, times)
        results[label] = r
        for cap in ("25x", "50x"):
            c = r[cap]
            flag = " *** GO ***" if c["go"] else ""
            print(f"  {label:<30} {cap:>5} | {c['mean']:7.3f} {c['ci_lo']:7.3f} {c['drop3']:7.3f} "
                  f"{c['f2logG']:+.4f} {c['bank']:10.0f}{flag}")
        print()

    # find break point (50x cap)
    print("=" * 100)
    print("  SLIP-SENSITIVITY (50x cap):")
    for s_lbl in ("slip +1.5%", "slip +10.0%", "slip +25.0%", "slip +50.0%"):
        c = results[s_lbl]["50x"]
        verdict = "GO" if c["go"] else f"NO-GO (CIlo={c['ci_lo']:.2f}, drop3={c['drop3']:.2f})"
        print(f"    {s_lbl:<14} -> mean {c['mean']:.3f}  CIlo {c['ci_lo']:.3f}  drop3 {c['drop3']:.3f}  => {verdict}")
    print("=" * 100)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
