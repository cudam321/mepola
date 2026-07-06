#!/usr/bin/env python3
"""Stage 14 — UN-TRUNCATED full-denominator channel backtest (the ANSEM re-test).

Motivation: prior channel backtests (stage6/9) priced each call over a 30d window measured
from the run date, so calls near the end of the corpus — and any LATE revival like $ANSEM
(9cRCn..., which 1000x'd on 2026-06-30, ~14d after its 2026-06-16 call) — were TRUNCATED.
This re-runs every first-call token with price data extended to TODAY, so tails are counted
at their real peak, then asks the only question that matters:

    Across the FULL denominator (all calls, winners AND the hundreds of zeros), under a
    disciplined moonbag and a diamond-hand hold, with REALISTIC liquidity-capped exits —
    does a follower of @your_channel come out net +EV?

Honest gating (RESEARCH.md): an EXECUTABLE policy must have bootstrap-CI lower bound > 1,
survive dropping the top-3 tokens, compound at f=2% (E[log] > 0), AND survive a realistic
liquidity cap. Reports uncapped + a cap sweep so the break-even realizability is explicit.
Avoids the 4 known artifacts: entry is the max-high in the latency window +slip (no wick / no
lookahead); the bankroll is a single chronological pass (no resampling-with-replacement); the
tail multiple is liquidity-capped; CI_lower + drop-top3 are printed beside every point mean.

    set -a && . ./.env && set +a && PYTHONPATH=src python3 scripts/stage14_untruncated.py
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
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
from memebot.models import PriceSeries  # noqa: E402
from stage4_powerlaw import P_MOON, P_HOLD  # noqa: E402

# Pin "now" so cache keys are stable across resumes (today, UTC).
NOW = datetime(2026, 6, 30, 12, 0, 0, tzinfo=timezone.utc)
LAT_S = 60.0           # channel-follower read->react latency
HOLD_DAYS_EXT = 45     # moonbag horizon (generous; un-truncates ANSEM-style day-14 revivals)
MIN_FETCH_H = 12       # minute-resolution window near entry (must be < 16h)
CAPS = [10.0, 25.0, 50.0, 100.0, float("inf")]   # realizable-exit multiple caps to sweep
GAS = 0.6              # round-trip fixed solana cost per trade, USD (negligible but modelled)

# the 3 #ANSEM ("The Black Bull") contracts the channel called on 2026-06-16
ANSEM = {
    "6KDh3wLSZMg37nnU7prtKZr7Rut7WQGSf33Vp1G7pump": "ANSEM-A (peaked ~10x, round-tripped)",
    "9cRCn9rGT8V2imeM2BaKs13yhMEais3ruM3rPvTGpump": "ANSEM-B (the ~1000x runner)",
    "Cx83EqERns2VuiKrkwHegTuECVqZebsNhJ3dCU8CucWG": "ANSEM-C (peaked ~2x, dead)",
}


def series_to_today(client, mint: str, t0: datetime) -> PriceSeries:
    """Minute candles for the first 12h (fine entry) + coarser candles out to min(t0+45d, NOW)."""
    end = min(t0 + timedelta(days=HOLD_DAYS_EXT), NOW)
    mn = client.get_price_series(mint, t0 - timedelta(minutes=5), t0 + timedelta(hours=MIN_FETCH_H))
    rest_start = t0 + timedelta(hours=MIN_FETCH_H)
    rest = client.get_price_series(mint, rest_start, end) if end > rest_start else PriceSeries(mint, None, "hour", 1, [])
    boundary = mn.candles[-1].ts if mn.candles else t0
    candles = list(mn.candles) + [c for c in rest.candles if c.ts > boundary]
    candles.sort(key=lambda c: c.ts)
    return PriceSeries(mint=mint, pool=None, timeframe="mixed", aggregate=1, candles=candles)


def entry_fill(series: PriceSeries, t: datetime):
    """Channel-follower fill: worst (max-high) price in the 90s reaction window, +1.5% slip.
    No wick-catching, no lookahead — you buy the high you could actually have hit."""
    win = [c for c in series.candles if t <= c.ts <= t + timedelta(seconds=90)]
    if win:
        return max(c.high for c in win) * 1.015
    prior = [c for c in series.candles if c.ts <= t]
    return prior[-1].high * 1.015 if prior else None


# ---- power-law-native metrics (formulas verbatim from stage3/6) ----
def hill_alpha(mults, tail_frac=0.10):
    a = np.sort(np.asarray([m for m in mults if m > 0], dtype=float))[::-1]
    k = max(5, int(len(a) * tail_frac))
    if len(a) <= k:
        return float("nan")
    return float(1.0 / np.mean(np.log(a[:k] / a[k])))


def mean_ci(mults, n=5000, seed=0):
    a = np.asarray(mults, dtype=float)
    if len(a) < 2:
        return float(a.mean()), float("nan"), float("nan")
    rng = np.random.default_rng(seed)
    bs = a[rng.integers(0, len(a), size=(n, len(a)))].mean(axis=1)
    return float(a.mean()), float(np.percentile(bs, 2.5)), float(np.percentile(bs, 97.5))


def fixed_f_growth(mults, f=0.02):
    a = np.asarray(mults, dtype=float)
    return float(np.mean(np.log(np.maximum(1 + f * (a - 1), 1e-9))))


def opt_f_growth(mults):
    a = np.asarray(mults, dtype=float)
    best_f, best_g = 0.0, -1e9
    for f in np.linspace(0.005, 0.5, 60):
        g = float(np.mean(np.log(np.maximum(1 + f * (a - 1), 1e-9))))
        if g > best_g:
            best_g, best_f = g, f
    return best_f, best_g


def drop_top(mults, k):
    a = np.sort(np.asarray(mults, dtype=float))
    return float(a[:-k].mean()) if len(a) > k else float("nan")


def cap_mults(mults, cap):
    return [min(m, cap) for m in mults]


def single_pass_bankroll(mults, times, f=0.02, cap=float("inf"), start=500.0):
    """Honest bankroll: each token traded ONCE, in chronological order, fractional sizing,
    liquidity-capped multiple, fixed gas. No resampling."""
    order = np.argsort(times)
    B = start
    for j in order:
        if B <= 0:
            break
        stake = f * B
        m = min(mults[j], cap)
        B = B - stake - GAS + stake * m
    return float(B)


def report(tag, mults, times):
    if len(mults) < 10:
        print(f"  [{tag}] n={len(mults)} too few"); return None
    out = {"tag": tag, "n": len(mults)}
    a = np.asarray(mults, dtype=float)
    alpha = hill_alpha(mults)
    win = float((a > 1).mean()) * 100
    p_total_loss = float((a < 0.1).mean()) * 100
    print(f"  [{tag}]  n={len(mults)}  alpha={alpha:.2f}  win={win:.0f}%  ~total-loss={p_total_loss:.0f}%  "
          f"median={np.median(a):.3f}x  MFE-ish max={a.max():.0f}x")
    out.update(alpha=alpha, win_pct=win, total_loss_pct=p_total_loss, median=float(np.median(a)), max=float(a.max()))
    out["by_cap"] = {}
    print(f"        {'cap':>6} | {'mean':>7} {'CIlo':>7} {'CIhi':>7} | {'drop1':>7} {'drop3':>7} | "
          f"{'f=2% logG':>9} | {'$500@2%':>9} {'$500@5%':>9}")
    for cap in CAPS:
        cm = cap_mults(mults, cap)
        m, lo, hi = mean_ci(cm)
        d1, d3 = drop_top(cm, 1), drop_top(cm, 3)
        g2 = fixed_f_growth(cm, 0.02)
        bank2 = single_pass_bankroll(cm, times, f=0.02, cap=float("inf"))
        bank5 = single_pass_bankroll(cm, times, f=0.05, cap=float("inf"))
        clabel = "inf" if cap == float("inf") else f"{cap:.0f}x"
        flag = "  <== compounds" if g2 > 0 and lo > 1 else ""
        print(f"        {clabel:>6} | {m:>7.3f} {lo:>7.3f} {hi:>7.3f} | {d1:>7.3f} {d3:>7.3f} | "
              f"{g2:>+9.4f} | {bank2:>9.0f} {bank5:>9.0f}{flag}")
        out["by_cap"][clabel] = dict(mean=m, ci_lo=lo, ci_hi=hi, drop1=d1, drop3=d3,
                                     f2_logG=g2, bank_500_f2=bank2, bank_500_f5=bank5)
    return out


def main() -> int:
    calls = first_call_per_mint(load_corpus_json(str(ROOT / "runs" / "your_channel_corpus.json")))
    calls = sorted(calls, key=lambda s: s.posted_at)
    client = CachedPriceClient(JupiterChartsClient(min_interval=0.4), str(ROOT / "data_cache" / "jupiter_untrunc"))
    print("=" * 104)
    print(f"  STAGE 14 — UN-TRUNCATED full-denominator @your_channel backtest | {len(calls)} calls | "
          f"horizon=min(call+{HOLD_DAYS_EXT}d, today) | lat={LAT_S:.0f}s")
    print("=" * 104)

    rows = []  # (mint, posted_at, moon_mult, hold_mult, mfe)
    for i, s in enumerate(calls):
        if i % 50 == 0:
            print(f"\r  pricing {i}/{len(calls)} (cache {client.hits}h/{client.misses}m)", end="", file=sys.stderr)
        if not s.mint:
            continue
        t = s.posted_at + timedelta(seconds=LAT_S)
        try:
            ser = series_to_today(client, s.mint, s.posted_at)
        except Exception:
            ser = None
        fill = entry_fill(ser, t) if (ser and ser.candles) else None
        if fill is None or fill <= 0:
            rows.append((s.mint, s.posted_at, 0.0, 0.0, 0.0)); continue  # uncharted/dead -> total loss
        fwd = [c.high for c in ser.candles if c.ts >= t]
        mfe = (max(fwd) / fill) if fwd else 0.0
        moon = simulate_exit(ser, fill, t, P_MOON)
        hold = simulate_exit(ser, fill, t, P_HOLD)
        rows.append((s.mint, s.posted_at, moon, hold, mfe))
    print("\r" + " " * 60 + "\r", end="", file=sys.stderr)

    times = np.array([r[1].timestamp() for r in rows])
    moon = [r[2] for r in rows]
    hold = [r[3] for r in rows]
    mfe = [r[4] for r in rows]

    print(f"\n  priced {len(rows)} calls | cache {client.hits}h/{client.misses}m | "
          f"uncharted/dead-at-entry: {sum(1 for m in mfe if m == 0.0)}")

    print("\n  --- the 3 #ANSEM contracts under the mechanical policies (what a follower ACTUALLY captured) ---")
    for mint, label in ANSEM.items():
        r = next((x for x in rows if x[0] == mint), None)
        if r:
            print(f"    {label:38} MFE(peak)={r[4]:>8.1f}x   P_MOON(60% trail)={r[2]:>8.2f}x   P_HOLD(diamond)={r[3]:>8.2f}x")
        else:
            print(f"    {label:38} (not a first-call in corpus)")

    results = {"n_calls": len(rows), "horizon_days": HOLD_DAYS_EXT, "asof": NOW.isoformat(),
               "uncharted_or_dead": sum(1 for m in mfe if m == 0.0), "policies": {}}

    print("\n  === FULL SAMPLE ===")
    print("\n  MFE = perfect-foresight peak exit (UPPER BOUND, not executable):")
    results["policies"]["MFE_upperbound"] = report("MFE perfect-exit", mfe, times)
    print("\n  P_MOON = disciplined moonbag (sell 50% @2x, 60% trail armed@2x, 14d time-stop, no hard stop):")
    results["policies"]["P_MOON"] = report("P_MOON moonbag", moon, times)
    print("\n  P_HOLD = diamond-hand buy-and-hold to horizon (the pure power-law bet):")
    results["policies"]["P_HOLD"] = report("P_HOLD diamond", hold, times)

    # time-split robustness (first 70% of calls by date vs last 30%)
    n = len(rows); cut = int(n * 0.7)
    print("\n  === TIME-SPLIT (does any edge hold in BOTH halves?) ===")
    for half, lo, hi in [("EARLY 70%", 0, cut), ("LATE 30%", cut, n)]:
        idx = list(range(lo, hi))
        mm = [moon[j] for j in idx]; hh = [hold[j] for j in idx]; tt = times[idx]
        print(f"\n  [{half}] calls {lo}-{hi}")
        report(f"{half} P_MOON", mm, tt)
        report(f"{half} P_HOLD", hh, tt)

    (ROOT / "runs" / "stage14_untruncated.json").write_text(json.dumps(results, indent=2))
    print("\n  saved -> runs/stage14_untruncated.json")

    # ---- verdict ----
    print("\n" + "=" * 104)
    print("  GATE: an EXECUTABLE policy passes only if, at a REALISTIC cap (<=50x): CIlo>1 AND drop3>1 "
          "AND f=2% logG>0 AND $500 grows.")

    def passes(res):
        if not res:
            return False
        for clabel in ("25x", "50x"):
            c = res["by_cap"][clabel]
            if c["ci_lo"] > 1 and c["drop3"] > 1 and c["f2_logG"] > 0 and c["bank_500_f2"] > 500:
                return True
        return False

    moon_go = passes(results["policies"]["P_MOON"])
    hold_go = passes(results["policies"]["P_HOLD"])
    print(f"  P_MOON (executable, disciplined): {'*** GO ***' if moon_go else 'NO-GO'}")
    print(f"  P_HOLD (executable, diamond-hand): {'*** GO ***' if hold_go else 'NO-GO'}")
    print("=" * 104)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
