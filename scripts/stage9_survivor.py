#!/usr/bin/env python3
"""Stage 9 — SURVIVOR second-leg: a fundamentally different entry regime.

Every prior test entered at/near the launch or call (the sniper-dominated PEAK) -> E[M]<1. Here we
only trade tokens that GRADUATED (survivorship as a risk FILTER, not a bias), and enter at a CALM
post-graduation moment instead of a local top, then ride the uncapped 30d moonbag for the second-leg
tail. Several executable, no-lookahead entry triggers are compared. Power-law-native eval (mean +
Hill alpha + optimal-f log-growth). The question: is there ANY entry regime with E[M] > 1?

    set -a && . ./.env && set +a && PYTHONPATH=src python3 scripts/stage9_survivor.py
"""

from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from memebot.data.dune import DuneClient  # noqa: E402
from memebot.data.cache import CachedPriceClient  # noqa: E402
from memebot.data.jupiter import JupiterChartsClient  # noqa: E402
from memebot.analysis.exit_sim import simulate_exit  # noqa: E402
from stage4_powerlaw import P_MOON  # noqa: E402
from stage6_channel_powerlaw import opt_f_growth, mean_ci, hill_alpha, portfolio_mc  # noqa: E402

CAP = 1200
ENTRY_SLIP = 1.03
HOLD_DAYS = 30
H = lambda h: timedelta(hours=h)  # noqa: E731
D = lambda d: timedelta(days=d)   # noqa: E731
WINDOWS = {"TRAIN": ("2026-01-01", "2026-04-01"), "OOS": ("2026-04-01", "2026-06-01")}


def grad_sql(ws, we):
    return (f"SELECT basemint AS mint, min(created_at) AS grad FROM pumpswap_solana.pools "
            f"WHERE is_valid_pool = true AND created_at >= TIMESTAMP '{ws}' AND created_at < TIMESTAMP '{we}' "
            f"GROUP BY basemint")


def parse_ts(s):
    from datetime import datetime, timezone
    s = s.replace("Z", "").replace(" UTC", "").strip().replace(" ", "T")
    if "." in s:
        s = s.split(".")[0]
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


def price_at(cands, t):
    prior = [c for c in cands if c.ts <= t]
    return prior[-1].close if prior else None


def entry_times(cands, grad):
    """Real-time, no-lookahead entry triggers -> {rule: entry_ts or None}."""
    out = {}
    e = [c for c in cands if c.ts >= grad + H(24)]
    out["grad+24h"] = e[0].ts if e else None
    # pullback X% from the running post-grad high within a window
    for tag, frac, days in (("pull50_7d", 0.5, 7), ("pull70_14d", 0.3, 14)):
        runhi, hit = None, None
        for c in cands:
            if c.ts < grad:
                continue
            if c.ts > grad + D(days):
                break
            runhi = c.high if runhi is None else max(runhi, c.high)
            if runhi and c.low <= frac * runhi:
                hit = c.ts
                break
        out[tag] = hit
    # momentum: break above the first-6h high (continuation) within 7d
    f6 = [c for c in cands if grad <= c.ts <= grad + H(6)]
    if f6:
        h6, hit = max(c.high for c in f6), None
        for c in cands:
            if c.ts <= grad + H(6):
                continue
            if c.ts > grad + D(7):
                break
            if c.high >= h6:
                hit = c.ts
                break
        out["mom_break6h"] = hit
    return out


def report(tag, mults):
    if len(mults) < 15:
        print(f"  [{tag}] n={len(mults)} (too few)"); return
    m, lo, hi = mean_ci(mults)
    f, g = opt_f_growth(mults)
    mc = portfolio_mc(mults, f, n_bets=min(200, len(mults)))
    print(f"  [{tag}] n={len(mults)} mean={m:.2f} (CI {lo:.2f}-{hi:.2f}) alpha={hill_alpha(mults):.2f} "
          f"win={np.mean(np.asarray(mults) > 1)*100:.0f}% | optF={f:.3f} logG={g:+.4f} "
          f"{'COMPOUNDS' if g > 0 else 'shrinks'} | MC200 P(profit)={mc['p_profit']*100:.0f}% median={mc['median']:.2f}")


def main() -> int:
    dune = DuneClient()
    client = CachedPriceClient(JupiterChartsClient(min_interval=0.4), str(ROOT / "data_cache" / "jupiter_surv"))
    rules = ["grad+24h", "pull50_7d", "pull70_14d", "mom_break6h"]
    print("=" * 100)
    print(f"  STAGE 9 — SURVIVOR second-leg | graduated tokens | calm post-grad entries | {HOLD_DAYS}d moonbag")
    print("=" * 100)
    for wname, (ws, we) in WINDOWS.items():
        rows = dune.run_sql(grad_sql(ws, we))["rows"]
        n_all = len(rows)
        if n_all > CAP:
            rng = np.random.default_rng(0)
            rows = [rows[i] for i in rng.choice(n_all, CAP, replace=False)]
        by_rule = {r: [] for r in rules}
        n_trig = {r: 0 for r in rules}
        for i, r in enumerate(rows):
            if i % 100 == 0:
                print(f"\r    {wname} {i}/{len(rows)} (cache {client.hits}h/{client.misses}m)", end="", file=sys.stderr)
            grad = parse_ts(r["grad"])
            try:
                ser = client.get_price_series(r["mint"], grad - H(1), grad + D(HOLD_DAYS + 8))
            except Exception:
                ser = None
            if not (ser and ser.candles):
                for rule in rules:
                    by_rule[rule].append(0.0)  # uncharted graduate -> total loss (survivorship-free)
                continue
            ets = entry_times(ser.candles, grad)
            for rule in rules:
                te = ets.get(rule)
                if te is None:
                    continue  # setup didn't trigger -> no trade (legit real-time skip)
                n_trig[rule] += 1
                px = price_at(ser.candles, te)
                fill = px * ENTRY_SLIP if px and px > 0 else None
                by_rule[rule].append(simulate_exit(ser, fill, te, P_MOON) if fill else 0.0)
        print(f"\r  [{wname}] graduated universe={n_all} priced={len(rows)}", file=sys.stderr)
        print(f"\n  === {wname} ===")
        for rule in rules:
            print(f"   (trigger fired on {n_trig[rule]}/{len(rows)} tokens)")
            report(rule, by_rule[rule])
    print("\n" + "=" * 100)
    print("  GO iff some entry rule has mean CI-lower>1 AND logG>0 OOS. Else the survivor regime joins the floor.")
    print("=" * 100)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
