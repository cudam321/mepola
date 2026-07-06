#!/usr/bin/env python3
"""Generate a small sample corpus from REAL Solana tokens to validate the Phase 0.5
pipeline end-to-end against live data (before the Telegram channel is connected).

Uses CANONICAL mints directly (we never trust ticker->mint: live testing showed
DexScreener returns copycat mints — the exact risk the research flagged). It also
anchors the synthetic call timestamp INSIDE each token's actually-returned OHLCV
coverage, because GeckoTerminal's free minute data for established tokens is sparse and
lagged. (Real memecoins have one dense pool around the call, so this anchoring is only a
demo convenience.) It prints the horizons that fit the coverage; pass them to the backtest.

    PYTHONPATH=src python3 scripts/make_sample_corpus.py --out runs/sample_corpus.json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from memebot.data.geckoterminal import GeckoTerminalClient  # noqa: E402

TOKENS = [
    ("WIF", "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm"),
    ("BONK", "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"),
]
POST_ENTRY_MIN = 60   # we want this many minutes of priced coverage after the call


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "runs" / "sample_corpus.json"))
    args = ap.parse_args(argv)

    now = datetime.now(timezone.utc)
    common_first: datetime | None = None
    common_last: datetime | None = None
    with GeckoTerminalClient() as gt:
        for tk, mint in TOKENS:
            s = gt.get_price_series(mint, now - timedelta(hours=12), now)
            if s.empty:
                print(f"  {tk}: no coverage; skipping", file=sys.stderr)
                continue
            f, l = s.candles[0].ts, s.candles[-1].ts
            print(f"  {tk}: coverage {f.isoformat()} .. {l.isoformat()} ({len(s.candles)} candles)", file=sys.stderr)
            common_first = f if common_first is None else max(common_first, f)
            common_last = l if common_last is None else min(common_last, l)

    if common_first is None or common_last is None:
        print("no coverage for any token", file=sys.stderr)
        return 1

    # Place the call so that POST_ENTRY_MIN minutes of coverage remain after it,
    # and it still sits after the (latest) coverage start.
    posted = common_last - timedelta(minutes=POST_ENTRY_MIN + 5)
    if posted < common_first:
        posted = common_first + timedelta(minutes=2)
    span_after = int((common_last - posted).total_seconds() // 60)
    fit = [h for h in ("30s", "5m", "15m", "30m", "1h", "4h") if _minutes(h) <= span_after]

    messages: list[dict] = []
    mid = 1000
    for tk, mint in TOKENS:
        mid += 1
        messages.append({"id": mid, "date": posted.isoformat(),
                         "text": f"\U0001f680 aping ${tk}\nCA: {mint}"})
    messages.append({"id": mid + 1, "date": posted.isoformat(), "text": "gm frens wagmi"})  # noise -> UNKNOWN

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"channel": "SAMPLE", "messages": messages}, indent=2))
    print(f"wrote {len(messages)} messages -> {out}", file=sys.stderr)
    print(f"posted_at={posted.isoformat()}  coverage_after={span_after}min", file=sys.stderr)
    print(f"SUGGESTED_HORIZONS={','.join(fit)}")
    return 0


def _minutes(h: str) -> float:
    return {"30s": 0.5, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240}[h]


if __name__ == "__main__":
    raise SystemExit(main())
