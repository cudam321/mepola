#!/usr/bin/env python3
"""Stage 23 — what do the WINNERS have in common? Reverse-engineer the best play from the data.

Take all 1263 calls, label winners vs losers, and measure how well each OBSERVABLE-AT-ENTRY feature
separates them (rank-AUC: 0.5 = useless, 1.0 = perfect). If some at-entry feature separates, that IS
the pattern to build on. For contrast, also score POST-entry momentum (first-hour move) — which we
know predicts but is uncapturable — to show exactly where the predictive power lives.

    set -a && . ./.env && set +a && PYTHONPATH=src:scripts python3 scripts/stage23_winner_discriminant.py
"""

from __future__ import annotations

import csv
import re
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
import stage14_untruncated as S  # noqa: E402

_MAG = {"K": 1e3, "M": 1e6, "B": 1e9, "": 1.0}
_SM = re.compile(r"(?:🧠\s*)?([0-9]+)\s*(?:SM|wallets|smart money)", re.I)
_LIQ = re.compile(r"Liquidity\s*:?\s*\$?\s*([0-9.]+)\s*([KMB])?", re.I)
_TOP = re.compile(r"Top Holders?\s*:?\s*([0-9.]+)\s*%", re.I)
_DEV = re.compile(r"Dev Migrations?\s*:?\s*([0-9]+)", re.I)


def post_features(text):
    f = extract_features(text)
    sm = _SM.search(text); liq = _LIQ.search(text); top = _TOP.search(text); dev = _DEV.search(text)
    return {
        "entry_mc": f["entry_mc"], "lateness": f["lateness_ratio"], "tse_h": f["time_since_entry_h"],
        "n_sm": float(sm.group(1)) if sm else None,
        "liq": (float(liq.group(1)) * _MAG[(liq.group(2) or "").upper()]) if liq else None,
        "top_holders": float(top.group(1)) if top else None,
        "dev_migrations": float(dev.group(1)) if dev else None,
    }


def auc(feat, label):
    """Rank-AUC: P(feature higher for a winner than a loser). 0.5=no separation."""
    x = np.array([v for v, l in zip(feat, label) if v is not None and not np.isnan(v)])
    y = np.array([l for v, l in zip(feat, label) if v is not None and not np.isnan(v)])
    if y.sum() < 5 or (len(y) - y.sum()) < 5:
        return None, len(y)
    order = np.argsort(x)
    ranks = np.empty(len(x)); ranks[order] = np.arange(1, len(x) + 1)
    pos = ranks[y == 1].sum()
    n1 = y.sum(); n0 = len(y) - n1
    a = (pos - n1 * (n1 + 1) / 2) / (n1 * n0)
    return float(a), len(y)


def main() -> int:
    rows = {r["mint"]: r for r in csv.DictReader(open(ROOT / "runs" / "stage14_pertoken.csv"))}
    calls = first_call_per_mint(load_corpus_json(str(ROOT / "runs" / "your_channel_corpus.json")))
    client = CachedPriceClient(JupiterChartsClient(min_interval=0.4), str(ROOT / "data_cache" / "jupiter_untrunc"))

    data = []
    for i, s in enumerate(calls):
        if not s.mint or s.mint not in rows:
            continue
        if i % 150 == 0:
            print(f"\r  {i}/{len(calls)}", end="", file=sys.stderr)
        r = rows[s.mint]
        d = dict(moon=float(r["moon"]), mfe=float(r["mfe"]))
        d.update(post_features(s.raw_text))
        # post-entry momentum (uncapturable, for contrast)
        t = s.posted_at + timedelta(seconds=S.LAT_S)
        try:
            ser = S.series_to_today(client, s.mint, s.posted_at)
        except Exception:
            ser = None
        if ser and ser.candles:
            f0 = S.entry_fill(ser, t)
            if f0 and f0 > 0:
                h1 = [c for c in ser.candles if t <= c.ts <= t + timedelta(hours=1)]
                d["h1_max_POSTENTRY"] = (max(c.high for c in h1) / f0) if h1 else None
                d["h6_max_POSTENTRY"] = (max(c.high for c in ser.candles if t <= c.ts <= t + timedelta(hours=6)) / f0) if ser.candles else None
        data.append(d)
    print("\r" + " " * 30 + "\r", end="", file=sys.stderr)

    moon = np.array([min(d["moon"], 50.0) for d in data])
    mfe = np.array([d["mfe"] for d in data])
    win = (moon > 1.5).astype(int)        # moonbag actually paid (beyond costs)
    run = (mfe > 3.0).astype(int)         # token objectively ran 3x+ from entry
    n = len(data)
    print("=" * 92)
    print(f"  STAGE 23 — what separates WINNERS from losers? | n={n} | "
          f"WIN(moon>1.5x)={win.sum()} ({win.mean()*100:.0f}%) | RUN(mfe>3x)={run.sum()} ({run.mean()*100:.0f}%)")
    print("=" * 92)

    AT_ENTRY = ["entry_mc", "lateness", "tse_h", "n_sm", "liq", "top_holders", "dev_migrations"]
    POST = ["h1_max_POSTENTRY", "h6_max_POSTENTRY"]

    def block(title, feats, label):
        print(f"\n  {title}")
        res = []
        for fk in feats:
            a, nn = auc([d.get(fk) for d in data], label)
            if a is not None:
                res.append((abs(a - 0.5), a, fk, nn))
        for sep, a, fk, nn in sorted(res, reverse=True):
            tag = "  <-- separates" if abs(a - 0.5) > 0.1 else ("  (weak)" if abs(a - 0.5) > 0.05 else "  (≈useless)")
            print(f"      {fk:22} AUC={a:.3f}  (n={nn}){tag}")

    print("\n  ===== predicting WIN (moonbag > 1.5x) =====")
    block("OBSERVABLE AT ENTRY (what you could actually trade on):", AT_ENTRY, win)
    block("POST-ENTRY momentum (only knowable AFTER you'd have to buy):", POST, win)
    print("\n  ===== predicting RUN (token hit 3x+ from entry) =====")
    block("OBSERVABLE AT ENTRY:", AT_ENTRY, run)
    block("POST-ENTRY momentum:", POST, run)

    # winner vs loser medians on the at-entry features
    print("\n  winner vs loser medians (at-entry features, WIN label):")
    for fk in AT_ENTRY:
        wv = [d[fk] for d in data if d.get(fk) is not None and (min(d["moon"], 50) > 1.5)]
        lv = [d[fk] for d in data if d.get(fk) is not None and (min(d["moon"], 50) <= 1.5)]
        if len(wv) >= 5 and len(lv) >= 5:
            print(f"      {fk:18} winners={np.median(wv):10.3g}   losers={np.median(lv):10.3g}")
    print("=" * 92)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
