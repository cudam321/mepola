#!/usr/bin/env python3
"""Stage 20 — executable sub-selection: does picking the channel's LEAST-LATE calls win?

The early-seat test showed entering near smart money's entry has real juice but was lookahead. The
EXECUTABLE shadow: the channel PRINTS its lateness in every post (Current MC / Entry MC, Time Since
Entry, # smart-money wallets, liquidity, top-holder %, dev migrations, holding %). So apply a SECOND
filter, using ONLY post-time-observable fields, on top of the channel's own filter, then run the
power-law moonbag (P_MOON) on the un-truncated returns. Strict no-lookahead, train/OOS split, hard gate.

Reuses the un-truncated per-token moonbag returns from runs/stage14_pertoken.csv (entry at the POST).

    set -a && . ./.env && set +a && PYTHONPATH=src:scripts python3 scripts/stage20_selection.py
"""

from __future__ import annotations

import csv
import re
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from memebot.ingest.telegram_mcp import load_corpus_json, first_call_per_mint  # noqa: E402
from memebot.analysis.features import extract_features  # noqa: E402
import stage14_untruncated as S  # noqa: E402

CAP = 50.0
_MAG = {"K": 1e3, "M": 1e6, "B": 1e9, "": 1.0}
_SM = re.compile(r"(?:🧠\s*)?([0-9]+)\s*(?:SM|wallets|smart money)", re.I)
_LIQ = re.compile(r"Liquidity\s*:?\s*\$?\s*([0-9.]+)\s*([KMB])?", re.I)
_TOP = re.compile(r"Top Holders?\s*:?\s*([0-9.]+)\s*%", re.I)
_DEV = re.compile(r"Dev Migrations?\s*:?\s*([0-9]+)", re.I)


def feat(text):
    f = extract_features(text)
    sm = _SM.search(text); liq = _LIQ.search(text); top = _TOP.search(text); dev = _DEV.search(text)
    return dict(
        lateness=f["lateness_ratio"], tse=f["time_since_entry_h"], entry_mc=f["entry_mc"],
        n_sm=float(sm.group(1)) if sm else None,
        liq=(float(liq.group(1)) * _MAG[(liq.group(2) or "").upper()]) if liq else None,
        top=float(top.group(1)) if top else None,
        dev=float(dev.group(1)) if dev else None,
        stype=f["signal_type"],
    )


def gate(name, m, t):
    if len(m) < 15:
        print(f"    {name:34} n={len(m)} too few"); return False
    cm = S.cap_mults(m, CAP)
    mean, lo, hi = S.mean_ci(cm); d3 = S.drop_top(cm, 3); g2 = S.fixed_f_growth(cm, 0.02)
    bank = S.single_pass_bankroll(cm, t, 0.02, float("inf")); win = np.mean(np.array(m) > 1) * 100
    go = lo > 1 and d3 > 1 and g2 > 0 and bank > 500
    print(f"    {name:34} n={len(m):4d} mean={mean:6.3f} CIlo={lo:6.3f} drop3={d3:6.3f} "
          f"f2logG={g2:+.4f} win={win:3.0f}% $500->{bank:8.0f} {'*** GO ***' if go else ''}")
    return go


def main() -> int:
    rows = {r["mint"]: r for r in csv.DictReader(open(ROOT / "runs" / "stage14_pertoken.csv"))}
    calls = first_call_per_mint(load_corpus_json(str(ROOT / "runs" / "your_channel_corpus.json")))
    data = []
    for s in calls:
        if not s.mint or s.mint not in rows:
            continue
        r = rows[s.mint]
        ft = feat(s.raw_text)
        data.append(dict(mint=s.mint, ts=s.posted_at.timestamp(), moon=float(r["moon"]),
                         hold=float(r["hold"]), mfe=float(r["mfe"]), **ft))
    data.sort(key=lambda d: d["ts"])
    n = len(data)
    cut = int(n * 0.7)
    train, oos = data[:cut], data[cut:]
    print("=" * 100)
    print(f"  STAGE 20 — executable channel sub-selection | n={n} | train={len(train)} oos={len(oos)} | P_MOON @{CAP:.0f}x cap")
    print("=" * 100)

    print(f"\n  baseline (ALL calls):")
    gate("all TRAIN", [d["moon"] for d in train], np.array([d["ts"] for d in train]))
    gate("all OOS", [d["moon"] for d in oos], np.array([d["ts"] for d in oos]))

    # single-feature selection: pick best train threshold, apply to OOS (no lookahead)
    feats = [("lateness", False), ("entry_mc", False), ("tse", False), ("n_sm", True),
             ("liq", False), ("top", False), ("dev", False)]
    print(f"\n  single-feature selection (keep the favorable tail; threshold LEARNED on train, APPLIED to OOS):")
    for fname, high_good in feats:
        tr = [d for d in train if d.get(fname) is not None]
        if len(tr) < 40:
            print(f"    {fname:12} (only {len(tr)} train have it — skip)"); continue
        vals = np.array([d[fname] for d in tr])
        # try keeping the top/bottom 20/33/50% by the feature; pick the train-best by moonbag EV, report OOS
        best = None
        for q in (0.2, 0.33, 0.5):
            thr = np.quantile(vals, 1 - q if high_good else q)
            sel = (lambda v: v >= thr) if high_good else (lambda v: v <= thr)
            trsub = [d["moon"] for d in tr if sel(d[fname])]
            ev = np.mean(S.cap_mults(trsub, CAP)) if trsub else 0
            if best is None or ev > best[0]:
                best = (ev, q, thr, sel)
        _, q, thr, sel = best
        oos_sub = [(d["moon"], d["ts"]) for d in oos if d.get(fname) is not None and sel(d[fname])]
        side = "high" if high_good else "low"
        print(f"    [{fname} {side} {int(q*100)}%, thr={thr:.3g}]")
        if oos_sub:
            gate(f"  -> OOS {fname}", [x[0] for x in oos_sub], np.array([x[1] for x in oos_sub]))

    # the theory pick: low lateness (caught early) — the executable 'early seat'
    print(f"\n  THEORY PICK — low lateness (channel caught it early), graded:")
    haveL = [d for d in data if d.get("lateness") and 1.0 <= d["lateness"] < 1e4]
    haveL.sort(key=lambda d: d["lateness"])
    for label, lo_q, hi_q in [("lateness<1.5x", 0, None), ("lateness<2x", 0, None), ("least-late 20%", 0, 0.2),
                              ("least-late 33%", 0, 0.33)]:
        if label.startswith("lateness<"):
            thr = float(label.split("<")[1].rstrip("x"))
            sub = [d for d in haveL if d["lateness"] < thr]
        else:
            k = int(len(haveL) * hi_q)
            sub = haveL[:k]
        gate(label, [d["moon"] for d in sub], np.array([d["ts"] for d in sub]))

    # combined rule: low lateness AND small entry mcap (earliest in token life)
    print(f"\n  COMBINED — low lateness AND low entry mcap (train-tuned, OOS-tested):")
    trc = [d for d in train if d.get("lateness") and d.get("entry_mc")]
    ooc = [d for d in oos if d.get("lateness") and d.get("entry_mc")]
    if trc and ooc:
        lat_thr = np.quantile([d["lateness"] for d in trc], 0.5)
        mc_thr = np.quantile([d["entry_mc"] for d in trc], 0.5)
        sel = [d for d in ooc if d["lateness"] <= lat_thr and d["entry_mc"] <= mc_thr]
        gate(f"OOS lateness<={lat_thr:.2f} & mc<=${mc_thr/1e3:.0f}K", [d["moon"] for d in sel],
             np.array([d["ts"] for d in sel]))
    print("=" * 100)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
