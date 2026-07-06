#!/usr/bin/env python3
"""Stage 38 — is #1 an EDGE or a WINDOW-ARTIFACT? + the last untested lever (regime gating).

#1 = (dip=-50%, SL=-30%, TP1=3x sell33% then secure-and-ride, no re-entry), uncapped, from stage37.
Three decisive questions:
  (A) How much of OOS $500->$375 is ANSEM alone? Remove it; recompute bankroll/mean/drop3/bootstrap CI.
  (B) Split OOS into equal-count sub-windows: is ANY sub-window profitable besides the ANSEM one?
  (C) The ONE untested knob: market-REGIME gating. Only take a signal when the recent memecoin
      market is "hot", measured LOOKAHEAD-SAFE from the trailing 14d mean 24h-MFE of PRIOR calls
      that had already resolved (entry+24h < now). Does timing WHEN we play lift the floor?

    set -a && . ./.env && set +a && PYTHONPATH=src:scripts python3 scripts/stage38_ansem_dependence.py
"""
from __future__ import annotations
import sys
from pathlib import Path
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src")); sys.path.insert(0, str(ROOT / "scripts"))
from memebot.ingest.telegram_mcp import load_corpus_json, first_call_per_mint  # noqa: E402
from memebot.data.cache import CachedPriceClient  # noqa: E402
from memebot.data.jupiter import JupiterChartsClient  # noqa: E402
import stage14_untruncated as S  # noqa: E402

ANSEM = "9cRCn9rGT8V2imeM2BaKs13yhMEais3ruM3rPvTGpump"
W48 = 48 * 3600
DAY = 86400

# ---- #1's exact policy, lifted verbatim from stage37.sim (dip,sl,ftp,fsell,reentry) ----
# NOTE: sl is the stop LEVEL as a fraction of entry -> sl=0.7 means stop at 0.7x entry = a -30% stop.
# Config #1 (the leaderboard winner: OOS mean 1.387, drop3 0.787) is sl=0.7 (a true -30% SL), NOT 0.3.
def sim(H, L, C, T, sig, dip=0.5, sl=0.7, ftp=3.0, fsell=0.33, reentry=None):
    n = len(H)
    if dip == 0:
        start = 0; entry = sig * 1.01
    else:
        start = None
        for j in range(n):
            if T[j] - T[0] > W48: break
            if L[j] <= (1 - dip) * sig: start = j; entry = (1 - dip) * sig * 1.01; break
        if start is None: return None
    legs = []; i = start
    while i < n and len(legs) < 8:
        rem = 1.0; pr = 0.0; ntp = 0; lvl = ftp; sec = False; stp = False; expx = C[-1]; eidx = n - 1
        for j in range(i, n):
            if rem <= 1e-9: eidx = j; break
            if (not sec) and sl > 0 and L[j] <= sl * entry:
                pr += rem * sl * entry * 0.95; rem = 0; stp = True; expx = sl * entry; eidx = j; break
            while rem > 1e-9 and H[j] >= lvl * entry:
                s = min(fsell if ntp == 0 else 0.25 * rem, rem)
                pr += s * lvl * entry * 0.985; rem -= s; ntp += 1
                if ntp == 1: sec = True
                lvl = lvl * 2 if ntp < 5 else lvl * 3
        if rem > 1e-9: pr += rem * C[-1]
        legs.append(pr / entry)
        if not stp or reentry is None: break
        tgt = reentry * expx; k = eidx + 1
        while k < n and H[k] < tgt: k += 1
        if k >= n: break
        entry = tgt * 1.01; i = k
    return legs


def bank(mults, times, cap=float("inf")):
    return S.single_pass_bankroll(list(mults), np.asarray(times), 0.02, cap)


def summ(mults):
    a = np.array(mults)
    if len(a) < 5:
        return f"n={len(a)} (too few)"
    m, lo, hi = S.mean_ci(list(a)); d3 = S.drop_top(list(a), 3)
    return f"mean={m:.3f} CIlo={lo:.3f} drop3={d3:.3f} win={100*(a>1).mean():.0f}% n={len(a)}"


def main() -> int:
    calls = sorted([s for s in first_call_per_mint(load_corpus_json(str(ROOT / "runs" / "your_channel_fresh.json"))) if s.mint],
                   key=lambda s: s.posted_at)
    client = CachedPriceClient(JupiterChartsClient(min_interval=0.4), str(ROOT / "data_cache" / "jupiter_untrunc"))
    toks = []
    for k, s in enumerate(calls):
        if k % 200 == 0:
            print(f"\r  loading {k}/{len(calls)}", end="", file=sys.stderr)
        try:
            ser = S.series_to_today(client, s.mint, s.posted_at)
        except Exception:
            ser = None
        if not ser or not ser.candles:
            continue
        cds = [c for c in ser.candles if c.ts >= s.posted_at]
        if not cds or cds[0].open <= 0:
            continue
        H = np.array([c.high for c in cds]); L = np.array([c.low for c in cds])
        C = np.array([c.close for c in cds]); Tt = np.array([c.ts.timestamp() for c in cds])
        sig = cds[0].open; ts = Tt[0]
        legs = sim(H, L, C, Tt, sig)
        if not legs:
            continue
        mult = float(legs[0])          # #1 has no re-entry -> one leg per token
        # 24h max-favorable-excursion for the regime proxy (lookahead-safe: known 24h after entry)
        m24 = float((H[Tt - Tt[0] <= DAY] / sig).max()) if (Tt - Tt[0] <= DAY).any() else 1.0
        toks.append(dict(mint=s.mint, mult=mult, ts=ts, m24=m24))
    print("\r" + " " * 40 + "\r", end="", file=sys.stderr)

    toks.sort(key=lambda d: d["ts"])
    dates = [d["ts"] for d in toks]; cut = dates[int(len(dates) * 0.7)]
    train = [d for d in toks if d["ts"] < cut]
    oos = [d for d in toks if d["ts"] >= cut]
    print("=" * 100)
    print(f"  STAGE 38 — is #1 an edge or a window artifact?  {len(toks)} tokens ({len(train)} train / {len(oos)} OOS)")
    print("=" * 100)

    # ---------- (A) ANSEM dependence ----------
    oo_all = [d["mult"] for d in oos]; oo_t = [d["ts"] for d in oos]
    oo_ex = [d["mult"] for d in oos if d["mint"] != ANSEM]; oo_ext = [d["ts"] for d in oos if d["mint"] != ANSEM]
    ansem = next((d["mult"] for d in oos if d["mint"] == ANSEM), None)
    print("\n  (A) HOW MUCH OF #1 IS ANSEM?")
    print(f"      OOS as-is        : $500->{bank(oo_all, oo_t):>6.0f}   {summ(oo_all)}")
    print(f"      OOS minus ANSEM  : $500->{bank(oo_ex, oo_ext):>6.0f}   {summ(oo_ex)}")
    print(f"      ANSEM's own #1 multiple: {ansem:.1f}x   (train bankroll for the SAME policy: $500->{bank([d['mult'] for d in train],[d['ts'] for d in train]):.0f})")

    # ---------- (B) sub-window profitability ----------
    print("\n  (B) OOS SPLIT INTO 4 EQUAL-COUNT SUB-WINDOWS — which periods actually made money?")
    q = np.array_split(oos, 4)
    for i, seg in enumerate(q):
        seg = list(seg)
        mm = [d["mult"] for d in seg]; tt = [d["ts"] for d in seg]
        has_ansem = any(d["mint"] == ANSEM for d in seg)
        d0 = _fmt(seg[0]["ts"]); d1 = _fmt(seg[-1]["ts"])
        print(f"      window {i+1} [{d0}..{d1}] $500->{bank(mm, tt):>6.0f}   {summ(mm)}   {'<-- ANSEM here' if has_ansem else ''}")

    # ---------- (C) regime gating (the one untested lever) ----------
    print("\n  (C) MARKET-REGIME GATING — only trade #1 when the recent market is HOT")
    print("      regime = trailing 14d mean 24h-MFE of PRIOR calls resolved before entry (lookahead-safe)")
    # causal regime score per token
    for d in toks:
        prior = [p["m24"] for p in toks if p["ts"] + DAY < d["ts"] and p["ts"] >= d["ts"] - 14 * DAY]
        d["regime"] = float(np.mean(prior)) if prior else None
    print(f"      {'threshold':>18} | {'trades taken (OOS)':>18} | {'$500':>7} | stats")
    base_mm = oo_all
    print(f"      {'(no gate / #1)':>18} | {f'{len(base_mm)}/{len(oos)}':>18} | {bank(base_mm, oo_t):>7.0f} | {summ(base_mm)}")
    for thr in [1.5, 2.0, 3.0, 5.0]:
        taken = [d for d in oos if d["regime"] is not None and d["regime"] >= thr]
        drop_undef = [d for d in oos if d["regime"] is None]
        mm = [d["mult"] for d in taken]; tt = [d["ts"] for d in taken]
        has_ansem = any(d["mint"] == ANSEM for d in taken)
        note = " (ANSEM kept)" if has_ansem else " (ANSEM FILTERED OUT)"
        print(f"      regime>= {thr:<9.1f} | {f'{len(taken)}/{len(oos)}':>18} | {bank(mm, tt):>7.0f} | {summ(mm)}{note}")
    print("=" * 100)
    print("  READ: if (A) OOS-minus-ANSEM collapses, if (B) only the ANSEM window is green, and if (C)")
    print("  no threshold lifts drop3>1 without ANSEM, then #1 is a single-token tail bet, not a base to build on.")
    print("=" * 100)
    return 0


def _fmt(ts):
    import datetime as dt
    return dt.datetime.utcfromtimestamp(ts).strftime("%m-%d")


if __name__ == "__main__":
    raise SystemExit(main())
