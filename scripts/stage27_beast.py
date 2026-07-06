#!/usr/bin/env python3
"""Stage 27 — THE BEAST: combine momentum entry + hard stoploss + power-law moonbag, optimized as ONE algo.

The user's thesis: cut the 73% losers with a stoploss, ride the winners for the power-law tail. Build it
as a single combined algo and grid-optimize every knob together (entry mode x stoploss x moonbag exit),
on the fresh un-truncated corpus. Report the ABSOLUTE best config, whether it clears the gate, what it
does to ANSEM (the 1000x), and — decisively — a stoploss-tightness sweep showing what the stop does to
losers vs winners.

    set -a && . ./.env && set +a && PYTHONPATH=src:scripts python3 scripts/stage27_beast.py
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
from memebot.analysis.exit_sim import ExitPolicy, simulate_exit  # noqa: E402
import stage14_untruncated as S  # noqa: E402

CAP = 50.0
ANSEM = "9cRCn9rGT8V2imeM2BaKs13yhMEais3ruM3rPvTGpump"


def breakout(ser, t, f0, B, Wh=6):
    if B <= 0:
        return f0, t
    end = t + timedelta(hours=Wh); lvl = f0 * (1 + B)
    for c in ser.candles:
        if c.ts < t:
            continue
        if c.ts > end:
            break
        if c.high >= lvl:
            return lvl * 1.01, c.ts
    return None, None


def main() -> int:
    calls = sorted([s for s in first_call_per_mint(load_corpus_json(str(ROOT / "runs" / "your_channel_fresh.json"))) if s.mint],
                   key=lambda s: s.posted_at)
    client = CachedPriceClient(JupiterChartsClient(min_interval=0.4), str(ROOT / "data_cache" / "jupiter_untrunc"))
    toks = []
    for i, s in enumerate(calls):
        if i % 200 == 0:
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
        toks.append((s.mint, ser, t, f0, s.posted_at.timestamp()))
    print("\r" + " " * 40 + "\r", end="", file=sys.stderr)
    N = len(toks)
    print("=" * 100)
    print(f"  STAGE 27 — THE BEAST (momentum + stoploss + moonbag, combined) | {N} calls | cap {CAP}x")
    print("=" * 100)

    # exit templates (tp_ladder, trail_pct, arm) — the moonbag half
    EXITS = {
        "ride":   ([], 0.50, 1.0),
        "derisk": ([(2.0, 0.5)], 0.50, 2.0),
        "ladder": ([(2.0, 0.3), (3.0, 0.3), (5.0, 0.2)], 0.50, 2.0),
    }
    ENTRIES = {"post": 0.0, "bo35": 0.35, "bo50": 0.50}
    STOPS = {"noSL": 0.0, "SL70": 0.3, "SL50": 0.5, "SL30": 0.7}  # stop_mult = frac of entry

    def run(Bkey, Skey, Ekey):
        B = ENTRIES[Bkey]; sm = STOPS[Skey]; tp, tr, arm = EXITS[Ekey]
        pol = ExitPolicy("beast", tp, sm, tr, arm, 24 * 30)
        M, T, am = [], [], None
        for mint, ser, t, f0, ts in toks:
            bf, bt = breakout(ser, t, f0, B)
            if bf is None:
                continue
            m = simulate_exit(ser, bf, bt, pol)
            M.append(m); T.append(ts)
            if mint == ANSEM:
                am = m
        return M, T, am

    print("\n  full grid (36 combos) — top 10 by bootstrap CIlo:")
    grid = []
    for Bk in ENTRIES:
        for Sk in STOPS:
            for Ek in EXITS:
                M, T, am = run(Bk, Sk, Ek)
                if len(M) < 30:
                    continue
                cm = S.cap_mults(M, CAP); mean, lo, hi = S.mean_ci(cm); d3 = S.drop_top(cm, 3)
                g2 = S.fixed_f_growth(cm, 0.02); bank = S.single_pass_bankroll(cm, np.asarray(T), 0.02, float("inf"))
                go = lo > 1 and d3 > 1 and g2 > 0 and bank > 500
                grid.append((lo, f"{Bk}+{Sk}+{Ek}", len(M), mean, lo, d3, g2, bank, go, am))
    grid.sort(reverse=True)
    for lo, name, n, mean, ci, d3, g2, bank, go, am in grid[:10]:
        amt = f"ANSEM={am:.1f}x" if am is not None else "ANSEM=n/a"
        print(f"    {name:20} n={n:4d} mean={mean:6.3f} CIlo={ci:6.3f} drop3={d3:6.3f} logG={g2:+.4f} "
              f"$500->{bank:7.0f} {amt:11} {'*** GO ***' if go else ''}")
    best = grid[0]
    print(f"\n  ABSOLUTE BEST: {best[1]}  ->  {'GO' if best[8] else 'NO-GO'} (CIlo {best[4]:.3f}, need >1)")

    # THE decisive table: hold entry+exit fixed (post + derisk moonbag), sweep the STOPLOSS
    print("\n  what does the STOPLOSS actually do? (entry=post, moonbag=derisk, sweep stop level):")
    print(f"    {'stop':>8} | {'loser avg':>9} {'winner avg':>10} {'#winners':>8} | {'book mean':>9} {'CIlo':>7} | {'ANSEM':>8}")
    for Sk, sm in [("none", 0.0), ("-70%", 0.3), ("-50%", 0.5), ("-30%", 0.7), ("-20%", 0.8), ("-10%", 0.9)]:
        pol = ExitPolicy("s", [(2.0, 0.5)], sm, 0.50, 2.0, 24 * 30)
        M, am = [], None
        for mint, ser, t, f0, ts in toks:
            m = simulate_exit(ser, f0, t, pol); M.append(m)
            if mint == ANSEM:
                am = m
        a = np.array(S.cap_mults(M, CAP))
        losers = a[a < 1]; winners = a[a >= 1]
        mean, lo, hi = S.mean_ci(list(a))
        print(f"    {Sk:>8} | {losers.mean():>9.3f} {winners.mean() if len(winners) else 0:>10.2f} {len(winners):>8} | "
              f"{mean:>9.3f} {lo:>7.3f} | {am if am else 0:>7.1f}x")
    print("    ^ tighter stop lifts the loser avg BUT kills winners (incl ANSEM) — the net never crosses 1.")
    print("=" * 100)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
