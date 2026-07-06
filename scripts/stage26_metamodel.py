"""Stage 26: cross-validated meta-model.

Combine ALL weak at-entry channel fields into one regularized classifier to
predict WIN (moon>1.5x), with strict time-series CV (train early / test late).
Report OOS AUC, overfit gap, and — at the model's chosen probability threshold —
the OOS traded-set EV gate (bootstrap CIlo, drop-top-3, f=2% logG, $500 pass).

STRICT NO-LOOKAHEAD: every feature is parsed from the *first-call message itself*
(fields the channel prints at post time). Target comes from the price series.
"""
from __future__ import annotations
import re, sys, math
import numpy as np
from datetime import datetime

from memebot.ingest.telegram_mcp import load_corpus_json, first_call_per_mint
from memebot.analysis.features import signal_type
import stage14_untruncated as S

# ---------------------------------------------------------------- number parsing
_NUM = r"([0-9]+(?:\.[0-9]+)?)\s*([KMBkmb])?"
_MULT = {"K": 1e3, "M": 1e6, "B": 1e9, "": 1.0}

def _num(m):
    if not m:
        return None
    return float(m.group(1)) * _MULT[(m.group(2) or "").upper()]

def _age_h(s):
    """'4h ago' / '12m ago' / '6d ago' / '2min' -> hours."""
    m = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*(min|m|h|hr|hour|hours|d|day|days)\b", s, re.I)
    if not m:
        return None
    v = float(m.group(1)); u = m.group(2).lower()
    if u.startswith("min") or u == "m":
        return v / 60.0
    if u.startswith("d"):
        return v * 24.0
    return v  # hours

# ---------------------------------------------------------------- field regexes
_RE_MC_TOP   = re.compile(r"(?:^|\n)\s*(?:├\s*)?(?:MC|Market Cap)\s*:?\s*\$?\s*" + _NUM, re.I)
_RE_CURRENT  = re.compile(r"Current(?:\s*MC)?\s*:?\s*\$?\s*" + _NUM, re.I)
_RE_ENTRY    = re.compile(r"(?:Entry\s*MC|AVG ENTRY MC|Entry)\s*:?\s*\$?\s*" + _NUM, re.I)
_RE_SM_AT    = re.compile(r"SM\s*@\s*\$?\s*" + _NUM, re.I)
_RE_SM_MC    = re.compile(r"SM\s*\|\s*MC\s*:?\s*\$?\s*" + _NUM, re.I)       # '10 SM | MC: $562K'
_RE_NW_BRAIN = re.compile(r"🧠\s*([0-9]+)\s*SM")
_RE_NW_PAREN = re.compile(r"SMART MONEY\s*\(\s*([0-9]+)\s*wallets?\)", re.I)
_RE_NW_TOTW  = re.compile(r"Total(?:\s*Wallets)?\s*:?\s*([0-9]+)\s*wallets?", re.I)
_RE_NW_XSM   = re.compile(r"([0-9]+)\s*SM\b")
_RE_VOL      = re.compile(r"(?:Vol|TOTAL BUY|TOTAL VOLUME|Total Buy Vol|New Net Vol)\s*:?\s*\$?\s*" + _NUM, re.I)
_RE_HOLDPCT_A= re.compile(r"HOLDING PERCENT\s*:?\s*" + _NUM + r"\s*%", re.I)
_RE_HOLD_PAR = re.compile(r"Hold(?:ing)?\s*:?\s*\$?\s*[0-9.]+\s*[KMB]?\s*\(\s*([0-9]+(?:\.[0-9]+)?)\s*%\)", re.I)
_RE_HOLD_PIPE= re.compile(r"HOLDING\s*:?\s*([0-9]+(?:\.[0-9]+)?)\s*%\s*\|", re.I)
_RE_HOLD_PCT = re.compile(r"Holding\s*:?\s*([0-9]+(?:\.[0-9]+)?)\s*%", re.I)
_RE_HOLDVAL_A= re.compile(r"HOLDING VALUE\s*:?\s*\$?\s*" + _NUM, re.I)
_RE_HOLDVAL_P= re.compile(r"Hold(?:ing)?\s*:?\s*\$?\s*" + _NUM + r"\s*\(", re.I)  # 'Hold: $7.96K (27%)'
_RE_HOLDVAL_X= re.compile(r"HOLDING\s*:?\s*[0-9.]+%\s*\|\s*\$?\s*" + _NUM, re.I)   # 'HOLDING: 38%|$11.92K'
_RE_LIQ      = re.compile(r"Liquidity\s*:?\s*\$?\s*" + _NUM, re.I)
_RE_TOPH     = re.compile(r"Top Holders\s*:?\s*" + _NUM + r"\s*%", re.I)
_RE_DEVMIG   = re.compile(r"Dev Migrations\s*:?\s*([0-9]+)", re.I)
_RE_ORGANIC  = re.compile(r"Organic Volume\s*:?\s*(🟢|🔴|green|red)", re.I)
_RE_NETPNL   = re.compile(r"NET PNL\s*:?\s*[+\-]?\$?\s*[0-9.]+\s*[KMB]?\s*\|\s*([+\-]?[0-9]+(?:\.[0-9]+)?)\s*%", re.I)
_RE_FIRSTBUY = re.compile(r"First Buy\s*:?\s*([^\|\n]+)", re.I)
_RE_TSE      = re.compile(r"Time Since Entry\s*:?\s*([^\|\n]+)", re.I)
_RE_GRAD     = re.compile(r"Grad(?:uated at)?\s*:?\s*([^\|\n]+)", re.I)
_RE_CREATED  = re.compile(r"Created at\s*:?\s*([^\|\n]+)", re.I)


def extract(text):
    """Return dict of raw at-entry fields (None where absent)."""
    # current MC: prefer explicit Current, else the top-level MC/Market Cap
    current = _num(_RE_CURRENT.search(text))
    if current is None:
        m = _RE_MC_TOP.search(text)
        current = _num(m) if m else None
    # smart-money entry MC
    entry = (_num(_RE_ENTRY.search(text)) or _num(_RE_SM_AT.search(text))
             or _num(_RE_SM_MC.search(text)))
    lateness = (current / entry) if (entry and current and entry > 0) else None

    nw = None
    for rx in (_RE_NW_BRAIN, _RE_NW_PAREN, _RE_NW_TOTW, _RE_NW_XSM):
        m = rx.search(text)
        if m:
            nw = int(m.group(1)); break

    vol = _num(_RE_VOL.search(text))

    holdpct = None
    for rx in (_RE_HOLDPCT_A, _RE_HOLD_PAR, _RE_HOLD_PIPE, _RE_HOLD_PCT):
        m = rx.search(text)
        if m:
            holdpct = float(m.group(1)); break
    holdval = None
    for rx in (_RE_HOLDVAL_A, _RE_HOLDVAL_X, _RE_HOLDVAL_P):
        m = rx.search(text)
        if m:
            holdval = _num(m); break

    liq = _num(_RE_LIQ.search(text))
    toph = None
    m = _RE_TOPH.search(text)
    if m:
        toph = float(m.group(1))
    devmig = None
    m = _RE_DEVMIG.search(text)
    if m:
        devmig = int(m.group(1))
    organic = None
    m = _RE_ORGANIC.search(text)
    if m:
        g = m.group(1)
        organic = 1.0 if (g == "🟢" or g.lower() == "green") else 0.0
    netpnl = None
    m = _RE_NETPNL.search(text)
    if m:
        netpnl = float(m.group(1))

    # token age: first-buy (or time-since-entry as fallback)
    age = None
    m = _RE_FIRSTBUY.search(text) or _RE_TSE.search(text)
    if m:
        age = _age_h(m.group(1))
    grad = None
    m = _RE_GRAD.search(text)
    if m:
        grad = _age_h(m.group(1))
    created = None
    m = _RE_CREATED.search(text)
    if m:
        created = _age_h(m.group(1))

    return dict(current_mc=current, entry_mc=entry, lateness=lateness, n_wallets=nw,
                sm_vol=vol, holding_pct=holdpct, holding_value=holdval, liquidity=liq,
                top_holders=toph, dev_migrations=devmig, organic_vol=organic,
                net_pnl=netpnl, age_h=age, grad_h=grad, created_h=created,
                signal_type=signal_type(text))


# ---------------------------------------------------------------- build dataset
def load_moon():
    d = {}
    with open("runs/stage_fresh_pertoken.csv") as f:
        header = f.readline().strip().split(",")
        idx = {c: i for i, c in enumerate(header)}
        for line in f:
            p = line.strip().split(",")
            if len(p) < len(header):
                continue
            d[p[idx["mint"]]] = float(p[idx["moon"]])
    return d


def build():
    sigs = load_corpus_json("runs/your_channel_fresh.json")
    fc = first_call_per_mint(sigs)
    moon = load_moon()
    rows = []
    for s in fc:
        if s.mint not in moon:
            continue
        f = extract(s.raw_text)
        f["mint"] = s.mint
        f["posted_at"] = s.posted_at
        f["moon"] = moon[s.mint]
        rows.append(f)
    rows.sort(key=lambda r: r["posted_at"])
    return rows


if __name__ == "__main__":
    rows = build()
    y = np.array([1 if r["moon"] > 1.5 else 0 for r in rows])
    print("n rows:", len(rows), "WIN rate (moon>1.5):", round(y.mean(), 4))
    # coverage
    keys = ["current_mc","entry_mc","lateness","n_wallets","sm_vol","holding_pct",
            "holding_value","liquidity","top_holders","dev_migrations","organic_vol",
            "net_pnl","age_h","grad_h","created_h"]
    print("\nfield coverage (non-null):")
    for k in keys:
        cov = sum(1 for r in rows if r[k] is not None)
        print(f"  {k:16s} {cov:5d} {100*cov/len(rows):5.1f}%")
    from collections import Counter
    print("signal_type:", Counter(r["signal_type"] for r in rows))
