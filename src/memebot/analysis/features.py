"""Extract structured fields from @your_channel message text.

The channel's format varies over time, so these are tolerant regexes. We pull the
fields that matter for the follower-EV question: the smart-money Entry MC, the
Current MC at post (-> lateness), time-since-entry (a second lateness axis), the
signal type, and — on PROFIT ALERT messages — the claimed multiple.
"""

from __future__ import annotations

import re
from typing import Any, Optional

_NUM = r"\$?\s*([0-9]+(?:\.[0-9]+)?)\s*([KMBkmb])?"


def _val(m: Optional[re.Match]) -> Optional[float]:
    if not m:
        return None
    n = float(m.group(1))
    suf = (m.group(2) or "").upper()
    return n * {"K": 1e3, "M": 1e6, "B": 1e9, "": 1.0}[suf]


_ENTRY = re.compile(r"(?:Entry MC|AVG ENTRY MC|Entry)\s*:?\s*" + _NUM, re.I)
_SM_AT = re.compile(r"SM\s*@\s*" + _NUM, re.I)
_CURRENT = re.compile(r"(?:Current MC|Current|Market Cap)\s*:?\s*" + _NUM, re.I)
_MC = re.compile(r"(?:^|[^A-Za-z])MC\s*:?\s*" + _NUM)
_TSE = re.compile(r"Time Since Entry\s*:?\s*([0-9]+(?:\.[0-9]+)?)\s*(min|m|h|hr|hour|hours|d|day|days)\b", re.I)
_PROFIT = re.compile(r"PROFIT[^0-9]{0,15}?([0-9]+(?:\.[0-9]+)?)\s*[xX]", re.I)


def extract_features(text: str) -> dict[str, Any]:
    entry = _val(_ENTRY.search(text)) or _val(_SM_AT.search(text))
    current = _val(_CURRENT.search(text)) or _val(_MC.search(text))
    lateness = (current / entry) if (entry and current and entry > 0) else None

    tse = _TSE.search(text)
    tse_h: Optional[float] = None
    if tse:
        v = float(tse.group(1))
        u = tse.group(2).lower()
        tse_h = v / 60 if u.startswith("m") else (v * 24 if u.startswith("d") else v)

    pm = _PROFIT.search(text)
    return {
        "entry_mc": entry,
        "current_mc": current,
        "lateness_ratio": lateness,
        "time_since_entry_h": tse_h,
        "profit_multiple": float(pm.group(1)) if pm else None,
        "signal_type": signal_type(text),
    }


def signal_type(text: str) -> str:
    t = text.upper()
    if "PROFIT" in t:
        return "profit"
    if "BUY MORE" in t:
        return "buymore"
    if "HOLDING" in t:
        return "holding"
    if "VOLUME SIGNAL" in t:
        return "volume"
    if "MAIN SIGNAL" in t or "🚀 MAIN" in t:
        return "main"
    if "SMART MONEY" in t:
        return "smartmoney"
    if "CTO" in t:
        return "cto"
    return "other"


def claimed_multiple_by_ticker(signals) -> dict[str, float]:
    """Map ticker -> max claimed PROFIT multiple across all profit-alert messages."""
    best: dict[str, float] = {}
    for s in signals:
        if not s.ticker:
            continue
        m = _PROFIT.search(s.raw_text)
        if m and "PROFIT" in s.raw_text.upper():
            mult = float(m.group(1))
            if mult > best.get(s.ticker, 0.0):
                best[s.ticker] = mult
    return best
