#!/usr/bin/env python3
"""Stage 19 — the EARLY SEAT test: enter at smart money's OWN entry, then run the power-law rule.

The channel tells us where/when smart money entered: "Entry MC: $X" + "Time Since Entry: N h". So we
anchor entry at t_smart = post_time - N hours (a PERFECT, zero-latency copy of smart money's entry — an
upper bound, since in real time you can't see it) and re-simulate P_MOON / P_HOLD / a TP-SL grid forward
to today, path-dependent (no invalid rescaling). Then compare, on the SAME tokens, to the LATE seat
(entering at the channel's post). If even a perfect early copy can't clear CIlo>1 + drop3>1 + compounding,
the early seat is dead too; if it does, the real-time wallet-watch version is worth building.

    set -a && . ./.env && set +a && PYTHONPATH=src:scripts python3 scripts/stage19_early_seat.py
"""

from __future__ import annotations

import json
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
from memebot.analysis.exit_sim import ExitPolicy, simulate_exit  # noqa: E402
import stage14_untruncated as S  # noqa: E402

MAX_TSE_H = 72.0  # only tokens whose smart-money entry is <=72h before the post (reliably fetchable)
CAPS = [10.0, 25.0, 50.0, float("inf")]


def agg(name, mults, times, gate=True):
    out = {"name": name, "n": len(mults)}
    for cap in CAPS:
        cm = S.cap_mults(mults, cap)
        m, lo, hi = S.mean_ci(cm)
        d3 = S.drop_top(cm, 3)
        g2 = S.fixed_f_growth(cm, 0.02)
        bank = S.single_pass_bankroll(cm, times, 0.02, float("inf"))
        cl = "inf" if cap == float("inf") else f"{cap:.0f}x"
        go = lo > 1 and d3 > 1 and g2 > 0 and bank > 500
        out[cl] = dict(mean=m, ci_lo=lo, drop3=d3, f2logG=g2, bank=bank, go=go)
        if gate:
            print(f"      {name:18} {cl:>4} | mean {m:7.3f} CIlo {lo:7.3f} drop3 {d3:7.3f} "
                  f"f2logG {g2:+.4f} $500->{bank:11.0f} {'*** GO ***' if go else ''}")
    return out


def main() -> int:
    calls = sorted(first_call_per_mint(load_corpus_json(str(ROOT / "runs" / "your_channel_corpus.json"))),
                   key=lambda s: s.posted_at)
    client = CachedPriceClient(JupiterChartsClient(min_interval=0.4), str(ROOT / "data_cache" / "jupiter_earlyseat"))

    early, late, times = {p: [] for p in ("mfe", "moon", "hold")}, {p: [] for p in ("moon", "hold")}, []
    lateness_check = []
    grid_early = {}
    GRID = [ExitPolicy(f"TP{L:g}x_SL{int((1-Sl)*100)}", [(L, 1.0)], Sl, 1.0, float("inf"), 1e9)
            for L in (2, 3, 5) for Sl in (0.5, 0.7)]
    GRID += [ExitPolicy("ladder_trail", [(2, .4), (3, .3), (5, .3)], 0.6, 0.4, 2.0, 24 * 14),
             S.P_MOON, S.P_HOLD]
    n_anchored = 0
    for i, s in enumerate(calls):
        if not s.mint:
            continue
        f = extract_features(s.raw_text)
        tse = f["time_since_entry_h"]
        if tse is None or not (0.0 <= tse <= MAX_TSE_H):
            continue
        n_anchored += 1
        if n_anchored % 25 == 0:
            print(f"\r  pricing {n_anchored} (cache {client.hits}h/{client.misses}m)", end="", file=sys.stderr)
        t_post = s.posted_at + timedelta(seconds=S.LAT_S)
        t_smart = s.posted_at - timedelta(hours=tse)
        try:
            ser_e = S.series_to_today(client, s.mint, t_smart)
            ser_l = S.series_to_today(client, s.mint, s.posted_at)
        except Exception:
            ser_e = ser_l = None
        fe = S.entry_fill(ser_e, t_smart + timedelta(seconds=S.LAT_S)) if (ser_e and ser_e.candles) else None
        fl = S.entry_fill(ser_l, t_post) if (ser_l and ser_l.candles) else None
        if fe is None or fe <= 0 or fl is None or fl <= 0:
            continue  # need both seats on the same token for apples-to-apples
        times.append(s.posted_at.timestamp())
        lateness_check.append(fl / fe)  # realized late/early price ratio (should track channel's Current/Entry)
        fwd = [c.high for c in ser_e.candles if c.ts >= t_smart + timedelta(seconds=S.LAT_S)]
        early["mfe"].append(max(fwd) / fe if fwd else 0.0)
        early["moon"].append(simulate_exit(ser_e, fe, t_smart + timedelta(seconds=S.LAT_S), S.P_MOON))
        early["hold"].append(simulate_exit(ser_e, fe, t_smart + timedelta(seconds=S.LAT_S), S.P_HOLD))
        late["moon"].append(simulate_exit(ser_l, fl, t_post, S.P_MOON))
        late["hold"].append(simulate_exit(ser_l, fl, t_post, S.P_HOLD))
        for pol in GRID:
            grid_early.setdefault(pol.name, []).append(simulate_exit(ser_e, fe, t_smart + timedelta(seconds=S.LAT_S), pol))
    print("\r" + " " * 50 + "\r", end="", file=sys.stderr)

    times = np.asarray(times)
    n = len(times)
    print("=" * 100)
    print(f"  STAGE 19 — EARLY SEAT (smart-money entry) vs LATE SEAT (channel post) | same {n} tokens | tse<= {MAX_TSE_H:.0f}h")
    print("=" * 100)
    lc = np.array(lateness_check)
    print(f"\n  anchor check: realized late/early price ratio median {np.median(lc):.2f}x "
          f"(should track the channel's reported Current/Entry ~3x) — confirms we really entered earlier/lower.")

    print("\n  LATE SEAT (enter at the channel's post):")
    res = {"n": n, "late": {}, "early": {}, "grid_early": {}}
    res["late"]["P_MOON"] = agg("late P_MOON", late["moon"], times)
    res["late"]["P_HOLD"] = agg("late P_HOLD", late["hold"], times)
    print("\n  EARLY SEAT (enter at smart money's own entry, path-dependent re-sim):")
    res["early"]["MFE"] = agg("early MFE", early["mfe"], times)
    res["early"]["P_MOON"] = agg("early P_MOON", early["moon"], times)
    res["early"]["P_HOLD"] = agg("early P_HOLD", early["hold"], times)

    print("\n  EARLY-SEAT TP/SL grid (path-dependent):")
    any_go = False
    for name, mults in grid_early.items():
        r = agg(name, mults, times, gate=False)
        res["grid_early"][name] = r
        passed = any(r[c]["go"] for c in ("25x", "50x"))
        any_go |= passed
        best = r["50x"]
        print(f"      {name:18} 50x | mean {best['mean']:7.3f} CIlo {best['ci_lo']:7.3f} drop3 {best['drop3']:7.3f} "
              f"f2logG {best['f2logG']:+.4f} $500->{best['bank']:11.0f} {'*** GO ***' if passed else ''}")

    (ROOT / "runs" / "stage19_early_seat.json").write_text(json.dumps(res, indent=2, default=str))
    early_moon_go = any(res["early"]["P_MOON"][c]["go"] for c in ("25x", "50x"))
    early_hold_go = any(res["early"]["P_HOLD"][c]["go"] for c in ("25x", "50x"))
    print("\n" + "=" * 100)
    print(f"  EARLY-SEAT VERDICT @ realistic <=50x cap: P_MOON {'GO' if early_moon_go else 'NO-GO'} | "
          f"P_HOLD {'GO' if early_hold_go else 'NO-GO'} | any TP/SL policy: {'GO' if any_go else 'NO-GO'}")
    print(f"  (n={n}; perfect zero-latency copy = UPPER BOUND. Real-time wallet-watch would be worse.)")
    print("=" * 100)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
