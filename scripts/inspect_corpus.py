#!/usr/bin/env python3
"""Inspect a pulled corpus: parse stats + representative samples, so we can see the
channel's real signal format and how well the parser reads it.

    uv run python scripts/inspect_corpus.py runs/<channel>_corpus.json
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from memebot.ingest.telegram_mcp import load_corpus_json  # noqa: E402
from memebot.models import SignalSide  # noqa: E402


def _short(t: str, n: int = 280) -> str:
    t = " ".join(t.split())
    return t if len(t) <= n else t[:n] + "…"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("corpus")
    ap.add_argument("--samples", type=int, default=8)
    args = ap.parse_args(argv)

    sigs = load_corpus_json(args.corpus)
    n = len(sigs)
    sides = Counter(s.side.value for s in sigs)
    with_mint = [s for s in sigs if s.mint]
    with_ticker = [s for s in sigs if s.ticker]
    tradable = [s for s in sigs if s.is_tradable]
    conf = Counter(round(s.parse_confidence, 1) for s in sigs)

    print(f"== {args.corpus} ==")
    print(f"messages:            {n}")
    print(f"side distribution:   {dict(sides)}")
    print(f"with mint:           {len(with_mint)}  ({len(with_mint)/n*100:.1f}%)")
    print(f"with ticker:         {len(with_ticker)}  ({len(with_ticker)/n*100:.1f}%)")
    print(f"tradable (buy+mint): {len(tradable)}  ({len(tradable)/n*100:.1f}%)")
    print(f"confidence buckets:  {dict(sorted(conf.items()))}")
    print(f"avg msg length:      {sum(len(s.raw_text) for s in sigs)//max(n,1)} chars")

    print("\n--- samples WITH a mint (call format) ---")
    for s in with_mint[:args.samples]:
        print(f"\n[{s.posted_at.date()}] id={s.message_id} side={s.side.value} mint={s.mint}")
        print("  ", _short(s.raw_text))
    if not with_mint:
        print("  (none — channel may not post Solana mint addresses)")

    print("\n--- general samples (every Nth message) ---")
    step = max(1, n // max(args.samples, 1))
    for s in sigs[::step][:args.samples]:
        print(f"\n[{s.posted_at.date()}] id={s.message_id} side={s.side.value} mint={'Y' if s.mint else '-'} ticker={s.ticker or '-'}")
        print("  ", _short(s.raw_text))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
