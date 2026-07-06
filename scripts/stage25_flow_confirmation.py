#!/usr/bin/env python3
"""Stage 25 — the FLOW-CONFIRMATION edge: read the channel's live re-accumulation stream.

The channel posts "🔵 WALLET BUY MORE" follow-ups after a call, with live flow: New Net Vol, New
Wallets, Total Buy Vol. Novel, no-lookahead strategy: enter on the call, then HOLD only if smart money
keeps buying (follow-up flow arrives within H hours); CUT the calls that get no follow-through. Tests:
  (1) does follow-up flow in the first H hours predict winners (AUC)?
  (2) does the flow-confirmed hold / cut-the-rest strategy beat baseline (EV, OOS, gate)?
Strictly causal: the hold/cut decision at t0+H uses only updates posted by t0+H.

    set -a && . ./.env && set +a && PYTHONPATH=src:scripts python3 scripts/stage25_flow_confirmation.py
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from memebot.ingest.telegram_mcp import load_corpus_json, first_call_per_mint  # noqa: E402
from memebot.parser.signal_parser import parse_message  # noqa: E402
from memebot.data.cache import CachedPriceClient  # noqa: E402
from memebot.data.jupiter import JupiterChartsClient  # noqa: E402
from memebot.analysis.exit_sim import ExitPolicy, simulate_exit  # noqa: E402
import stage14_untruncated as S  # noqa: E402

MAG = {"K": 1e3, "M": 1e6, "B": 1e9, "": 1.0}
_NETVOL = re.compile(r"New Net Vol\s*:?\s*[+\-]?\$?\s*([0-9.]+)\s*([KMB])?", re.I)
_NEWW = re.compile(r"New Wallets\s*:?\s*([0-9]+)", re.I)
_TRIG = re.compile(r"Trigger\s*:?\s*\+?([0-9]+)\s*new wallets.*?\+?\$?([0-9.]+)\s*([KMB])?", re.I)


def flow_of(text):
    nv = _NETVOL.search(text); nw = _NEWW.search(text); tg = _TRIG.search(text)
    netvol = float(nv.group(1)) * MAG[(nv.group(2) or "").upper()] if nv else 0.0
    neww = int(nw.group(1)) if nw else 0
    if tg:
        neww = max(neww, int(tg.group(1)))
        netvol = max(netvol, float(tg.group(2)) * MAG[(tg.group(3) or "").upper()])
    is_buymore = ("BUY MORE" in text.upper()) or bool(tg) or bool(nv) or bool(nw)
    return is_buymore, neww, netvol


def price_at(ser, t):
    pri = [c for c in ser.candles if c.ts <= t]
    return pri[-1].close if pri else None


def main() -> int:
    raw = json.load(open(ROOT / "runs" / "your_channel_corpus.json"))["messages"]
    # per-mint message timeline with flow
    tl = {}
    for m in raw:
        ts = datetime.fromtimestamp(int(m["date"]), tz=timezone.utc)
        sig = parse_message("c", int(m["id"]), ts, m["text"])
        if not sig.mint:
            continue
        bm, nw, nv = flow_of(m["text"])
        tl.setdefault(sig.mint, []).append((ts, bm, nw, nv))
    for k in tl:
        tl[k].sort()

    calls = sorted(first_call_per_mint(load_corpus_json(str(ROOT / "runs" / "your_channel_corpus.json"))),
                   key=lambda s: s.posted_at)
    client = CachedPriceClient(JupiterChartsClient(min_interval=0.4), str(ROOT / "data_cache" / "jupiter_untrunc"))
    P_MOON = S.P_MOON

    H_LIST = [3, 6, 24]
    rows = []
    for i, s in enumerate(calls):
        if not s.mint:
            continue
        if i % 150 == 0:
            print(f"\r  {i}/{len(calls)}", end="", file=sys.stderr)
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
        moon_full = simulate_exit(ser, f0, t, P_MOON)
        mfe = max((c.high for c in ser.candles if c.ts >= t), default=f0) / f0
        rec = {"mint": s.mint, "ts": s.posted_at.timestamp(), "f0": f0, "moon_full": moon_full, "mfe": mfe}
        # follow-up flow within each horizon (exclude the call message itself: ts strictly after t0)
        msgs = tl.get(s.mint, [])
        for H in H_LIST:
            end = s.posted_at + timedelta(hours=H)
            fu = [x for x in msgs if s.posted_at < x[0] <= end]
            rec[f"nfu_{H}"] = sum(1 for x in fu if x[1])
            rec[f"nw_{H}"] = sum(x[2] for x in fu)
            rec[f"nv_{H}"] = sum(x[3] for x in fu)
            # cut price at horizon (for the unconfirmed branch)
            rec[f"cut_{H}"] = (price_at(ser, end) or ser.candles[-1].close) / f0
        rows.append(rec)
    print("\r" + " " * 30 + "\r", end="", file=sys.stderr)

    n = len(rows)
    moon = np.array([min(r["moon_full"], 50.0) for r in rows])
    win = (moon > 1.5).astype(int)
    print("=" * 96)
    print(f"  STAGE 25 — flow-confirmation edge | n={n} | WIN(moon>1.5x)={win.sum()} ({win.mean()*100:.0f}%)")
    print("=" * 96)

    def auc(vals, label):
        x = np.asarray(vals, float); y = np.asarray(label, float)
        if y.sum() < 5 or len(y) - y.sum() < 5:
            return 0.5
        order = np.argsort(x); ranks = np.empty(len(x)); ranks[order] = np.arange(1, len(x) + 1)
        n1 = y.sum(); n0 = len(y) - n1
        return float((ranks[y == 1].sum() - n1 * (n1 + 1) / 2) / (n1 * n0))

    print("\n  does follow-up flow in the first H hours predict the winner? (AUC):")
    for H in H_LIST:
        for key, lbl in ((f"nfu_{H}", "n buy-more"), (f"nw_{H}", "new wallets"), (f"nv_{H}", "new net vol $")):
            print(f"    H={H:2}h  {lbl:14} AUC={auc([r[key] for r in rows], win):.3f}  "
                  f"(have-flow: {np.mean([r[f'nfu_{H}']>0 for r in rows])*100:.0f}% of calls)")

    def gate(name, m, t):
        if len(m) < 12:
            print(f"    {name:46} n={len(m)} too few"); return
        mean, lo, hi = S.mean_ci(m); d3 = S.drop_top(m, 3); g2 = S.fixed_f_growth(m, 0.02)
        bank = S.single_pass_bankroll(m, t, 0.02, float("inf")); wr = np.mean(np.array(m) > 1) * 100
        go = lo > 1 and d3 > 1 and g2 > 0 and bank > 500
        print(f"    {name:46} n={len(m):4d} mean={mean:6.3f} CIlo={lo:6.3f} drop3={d3:6.3f} "
              f"logG={g2:+.4f} win={wr:3.0f}% $500->{bank:7.0f} {'*** GO ***' if go else ''}")

    print("\n  STRATEGY: enter on call; if >=K buy-more by t0+H -> HOLD moonbag; else CUT at t0+H:")
    rows.sort(key=lambda r: r["ts"]); cut_i = int(n * 0.7)
    for H in H_LIST:
        for K in (1, 2):
            blended = []
            conf_only = []
            for r in rows:
                if r[f"nfu_{H}"] >= K:
                    blended.append((r["moon_full"], r["ts"])); conf_only.append(r["moon_full"])
                else:
                    blended.append((r[f"cut_{H}"], r["ts"]))
            gate(f"H={H}h K>={K}: hold-if-confirmed / cut-rest", [x[0] for x in blended], np.array([x[1] for x in blended]))
            # confirmed-only (the subset you'd actually hold)
            tconf = np.array([r["ts"] for r in rows if r[f"nfu_{H}"] >= K])
            gate(f"   -> confirmed-only subset moonbag", conf_only, tconf)

    # OOS the confirmed-only (H=6,K>=1)
    print("\n  OOS check (confirmed-only moonbag, H=6 K>=1):")
    oos = rows[cut_i:]
    co = [(r["moon_full"], r["ts"]) for r in oos if r["nfu_6"] >= 1]
    gate("OOS confirmed-only H=6", [x[0] for x in co], np.array([x[1] for x in co]))
    print("=" * 96)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
