#!/usr/bin/env python3
"""Stage 24 — FULL digestion of every channel field + the best algo it supports.

Extract EVERY labeled stat the channel posts (incl. the untested flow/quality fields: New Wallets,
New Net Vol, Total Buy Vol, Organic Volume flag, Net PnL, token age, smart-money conviction) plus
derived ratios, then rank each by how well it separates winners from losers at entry (rank-AUC).
Then build + OOS-test the best multi-feature selection, and describe the best executable algo.

    set -a && . ./.env && set +a && PYTHONPATH=src:scripts python3 scripts/stage24_full_digest.py
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
from memebot.analysis.features import extract_features, signal_type  # noqa: E402
import stage14_untruncated as S  # noqa: E402

MAG = {"K": 1e3, "M": 1e6, "B": 1e9, "": 1.0}


def money(label, text):
    m = re.search(label + r"\s*:?\s*[+\-]?\$?\s*([0-9.]+)\s*([KMB])?", text, re.I)
    return float(m.group(1)) * MAG[(m.group(2) or "").upper()] if m else None


def pct(label, text):
    m = re.search(label + r"\s*:?\s*([0-9.]+)\s*%", text, re.I)
    return float(m.group(1)) if m else None


def integer(label, text):
    m = re.search(label + r"\s*\(?\s*([0-9]+)", text, re.I)
    return int(m.group(1)) if m else None


def age_h(label, text):
    m = re.search(label + r"\s*:?\s*([0-9.]+)\s*(m|min|h|hr|d|day)", text, re.I)
    if not m:
        return None
    v = float(m.group(1)); u = m.group(2).lower()
    return v / 60 if u.startswith("m") else (v * 24 if u.startswith("d") else v)


def feats(text):
    f = extract_features(text)
    d = {
        "entry_mc": f["entry_mc"], "current_mc": f["current_mc"], "lateness": f["lateness_ratio"],
        "tse_h": f["time_since_entry_h"],
        "n_wallets": integer(r"SMART MONEY", text) or integer(r"TOTAL WALLETS", text),
        "new_wallets": integer(r"NEW WALLETS", text),
        "liquidity": money(r"Liquidity", text),
        "top_holders": pct(r"Top Holders", text),
        "dev_migrations": integer(r"Dev Migrations", text),
        "total_buy": money(r"TOTAL BUY VOL", text) or money(r"TOTAL BUY", text),
        "new_net_vol": money(r"NEW NET VOL", text),
        "total_volume": money(r"TOTAL VOLUME", text),
        "holding_pct": pct(r"HOLDING PERCENT", text) or pct(r"HOLDING", text),
        "holding_value": money(r"HOLDING VALUE", text),
        "avg_entry_mc": money(r"AVG ENTRY MC", text),
        "avg_sell_mc": money(r"AVG SELL MC", text),
        "net_pnl_pct": pct(r"NET PNL[^|]*\|", text) or pct(r"NET PNL", text),
        "created_age_h": age_h(r"Created at", text),
        "grad_age_h": age_h(r"Graduated at", text),
        "first_buy_age_h": age_h(r"First Buy", text),
    }
    # organic volume flag
    ov = re.search(r"Organic Volume\s*:?\s*(🟢|🟡|🟠|🔴)", text)
    d["organic_vol"] = {"🟢": 1.0, "🟡": 0.66, "🟠": 0.33, "🔴": 0.0}.get(ov.group(1)) if ov else None
    d["net_pnl_pos"] = 1.0 if (re.search(r"NET PNL\s*:?\s*\+", text)) else (0.0 if "NET PNL" in text.upper() else None)
    # derived
    nb = lambda a, b: (d[a] / d[b]) if (d.get(a) and d.get(b) and d[b] > 0) else None
    d["buy_per_wallet"] = nb("total_buy", "n_wallets")
    d["holding_retention"] = nb("holding_value", "total_buy")
    d["new_wallet_ratio"] = nb("new_wallets", "n_wallets")
    d["flow_vs_liq"] = nb("new_net_vol", "liquidity")
    d["buy_vs_liq"] = nb("total_buy", "liquidity")
    d["sigtype"] = signal_type(text)
    return d


def auc(vals, label):
    x = np.array([v for v, l in zip(vals, label) if v is not None and not (isinstance(v, float) and np.isnan(v))], dtype=float)
    y = np.array([l for v, l in zip(vals, label) if v is not None and not (isinstance(v, float) and np.isnan(v))], dtype=float)
    if y.sum() < 5 or (len(y) - y.sum()) < 5:
        return None, len(y)
    order = np.argsort(x); ranks = np.empty(len(x)); ranks[order] = np.arange(1, len(x) + 1)
    n1 = y.sum(); n0 = len(y) - n1
    return float((ranks[y == 1].sum() - n1 * (n1 + 1) / 2) / (n1 * n0)), len(y)


def main() -> int:
    rows = {r["mint"]: r for r in csv.DictReader(open(ROOT / "runs" / "stage14_pertoken.csv"))}
    calls = first_call_per_mint(load_corpus_json(str(ROOT / "runs" / "your_channel_corpus.json")))
    data = []
    for s in calls:
        if not s.mint or s.mint not in rows:
            continue
        r = rows[s.mint]
        d = feats(s.raw_text)
        d.update(mint=s.mint, ts=s.posted_at.timestamp(), moon=min(float(r["moon"]), 50.0), mfe=float(r["mfe"]))
        data.append(d)
    n = len(data)
    win = np.array([1 if d["moon"] > 1.5 else 0 for d in data])     # moonbag paid
    run = np.array([1 if d["mfe"] > 2.0 else 0 for d in data])      # token made a tradeable 2x+ move
    FEATS = [k for k in data[0] if k not in ("mint", "ts", "moon", "mfe", "sigtype")]

    print("=" * 96)
    print(f"  STAGE 24 — full field digestion | n={n} | WIN(moon>1.5x)={win.sum()} | RUN(mfe>2x)={run.sum()}")
    print("=" * 96)
    print("\n  EVERY field ranked by winner-discrimination (|AUC-0.5|), vs WIN and vs RUN:")
    print(f"    {'feature':22} {'cov':>5} {'AUC_win':>8} {'AUC_run':>8}")
    res = []
    for fk in FEATS:
        aw, nw = auc([d[fk] for d in data], win)
        ar, nr = auc([d[fk] for d in data], run)
        strength = max(abs((aw or .5) - .5), abs((ar or .5) - .5))
        res.append((strength, fk, aw, ar, nw))
    for strength, fk, aw, ar, nw in sorted(res, reverse=True):
        flag = "  <== SEPARATES" if strength > 0.12 else ("  (weak)" if strength > 0.07 else "")
        aws = f"{aw:.3f}" if aw is not None else "  -  "
        ars = f"{ar:.3f}" if ar is not None else "  -  "
        print(f"    {fk:22} {nw:5d} {aws:>8} {ars:>8}{flag}")

    # signal-type breakdown
    print("\n  win-rate (moon>1.5x) by signal type:")
    for st in sorted(set(d["sigtype"] for d in data)):
        sub = [d for d in data if d["sigtype"] == st]
        if len(sub) >= 15:
            wr = np.mean([1 if d["moon"] > 1.5 else 0 for d in sub]) * 100
            ev = np.mean([d["moon"] for d in sub])
            print(f"    {st:12} n={len(sub):4d}  win={wr:3.0f}%  moon-EV={ev:.3f}x")

    # take the top-3 separating features, build a simple OOS-validated selection
    top = [fk for strength, fk, aw, ar, nw in sorted(res, reverse=True)[:3]]
    print(f"\n  OOS test — select on the 3 best fields {top} (train thresholds -> apply to OOS):")
    data.sort(key=lambda d: d["ts"]); cut = int(n * 0.7)
    train, oos = data[:cut], data[cut:]
    for fk in top:
        tr = [d[fk] for d in train if d.get(fk) is not None]
        if len(tr) < 30:
            print(f"    {fk}: too sparse in train"); continue
        # decide direction from train AUC sign, keep favorable half
        aw, _ = auc([d[fk] for d in train], np.array([1 if d["moon"] > 1.5 else 0 for d in train]))
        hi_good = (aw or .5) >= .5
        thr = np.quantile(tr, 0.5)
        sel = [d for d in oos if d.get(fk) is not None and ((d[fk] >= thr) if hi_good else (d[fk] <= thr))]
        if len(sel) >= 12:
            m = [d["moon"] for d in sel]; t = np.array([d["ts"] for d in sel])
            mean, lo, hi = S.mean_ci(m); d3 = S.drop_top(m, 3); g2 = S.fixed_f_growth(m, 0.02)
            bank = S.single_pass_bankroll(m, t, 0.02, float("inf"))
            go = lo > 1 and d3 > 1 and g2 > 0 and bank > 500
            side = "high" if hi_good else "low"
            print(f"    OOS {fk:18} {side}-half  n={len(sel):3d} mean={mean:.3f} CIlo={lo:.3f} drop3={d3:.3f} "
                  f"logG={g2:+.4f} $500->{bank:.0f} {'*** GO ***' if go else ''}")
    print("=" * 96)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
