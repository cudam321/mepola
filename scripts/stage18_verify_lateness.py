#!/usr/bin/env python3
"""Stage 18 — VERIFY the user's objection: is "Current 3x Entry" a profit-UPDATE, not the call?

User's claim: the channel calls a token at a low entry, then later prints "Current: $3X" as a profit
update; so the lateness stat wrongly treats updates as the follower's entry — the follower actually got
in at the low entry.

This checks it against the data, three ways:
  (1) For EACH mint, find the channel's GENUINELY FIRST message (earliest ts, any type) and the first
      BUY message the backtest uses. Are they the same? Is the first message a real CALL or an UPDATE?
  (2) On that FIRST message: distribution of Current/Entry MC and "Time Since Entry". If the first post
      already shows Current >> Entry with time-since-entry > 0, the channel DETECTS late — the follower's
      earliest possible entry is the elevated price, exactly as modelled.
  (3) Full chronological message traces for a few tokens (incl. ANSEM-B) so you can SEE the sequence.

    set -a && . ./.env && set +a && PYTHONPATH=src:scripts python3 scripts/stage18_verify_lateness.py
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from memebot.ingest.telegram_mcp import load_corpus_json, first_call_per_mint  # noqa: E402
from memebot.parser.signal_parser import parse_message  # noqa: E402
from memebot.analysis.features import extract_features  # noqa: E402

ANSEM_B = "9cRCn9rGT8V2imeM2BaKs13yhMEais3ruM3rPvTGpump"


def main() -> int:
    import json
    raw = json.load(open(ROOT / "runs" / "your_channel_corpus.json"))["messages"]
    # attribute each message to its PRIMARY mint (same rule the backtest uses)
    timeline: dict[str, list] = {}
    for m in raw:
        ts = datetime.fromtimestamp(int(m["date"]), tz=timezone.utc)
        sig = parse_message("your_channel", int(m["id"]), ts, m["text"])
        if not sig.mint:
            continue
        f = extract_features(m["text"])
        timeline.setdefault(sig.mint, []).append(
            dict(ts=ts, type=f["signal_type"], entry=f["entry_mc"], current=f["current_mc"],
                 tse_h=f["time_since_entry_h"], profit=f["profit_multiple"], is_buy=sig.is_tradable))
    for mint in timeline:
        timeline[mint].sort(key=lambda d: d["ts"])

    first_buys = {s.mint: s for s in first_call_per_mint(load_corpus_json(str(ROOT / "runs" / "your_channel_corpus.json")))}

    print("=" * 100)
    print(f"  STAGE 18 — does the channel CALL at entry, or DETECT late? | {len(timeline)} mints")
    print("=" * 100)

    # (1) first message: is it a call or an update? does an earlier mention precede the backtest's first-BUY?
    first_is_update = 0
    earlier_mention = 0
    first_types = {}
    for mint, msgs in timeline.items():
        first = msgs[0]
        first_types[first["type"]] = first_types.get(first["type"], 0) + 1
        if first["type"] in ("profit", "buymore"):
            first_is_update += 1
        if mint in first_buys:
            fb_ts = first_buys[mint].posted_at
            if first["ts"] < fb_ts:
                earlier_mention += 1
    print("\n  (1) what TYPE is each token's FIRST message?")
    for t, c in sorted(first_types.items(), key=lambda x: -x[1]):
        print(f"        {t:12} {c:4d} ({c/len(timeline)*100:4.1f}%)")
    print(f"        first message is a PROFIT/BUY-MORE update: {first_is_update} ({first_is_update/len(timeline)*100:.1f}%)")
    print(f"        a non-BUY mention precedes the backtest's first-BUY entry: {earlier_mention} mints "
          f"(backtest would be later than first mention)")

    # (2) on the FIRST message: lateness + time-since-entry
    lateness, tse, genuine_early = [], [], 0
    for mint, msgs in timeline.items():
        first = msgs[0]
        if first["entry"] and first["current"] and first["entry"] > 0:
            lr = first["current"] / first["entry"]
            if 1.0 <= lr < 1e4:
                lateness.append(lr)
                if first["tse_h"] is not None:
                    tse.append(first["tse_h"])
                if lr < 1.3 and (first["tse_h"] or 0) < 1:
                    genuine_early += 1
    lateness = np.array(lateness); tse = np.array(tse)
    print(f"\n  (2) on each token's FIRST message (n={len(lateness)} with both Entry+Current MC):")
    print("        Current/Entry MC:  " + "  ".join(f"p{p}={np.percentile(lateness,p):.2f}x" for p in [10, 25, 50, 75, 90]))
    if len(tse):
        print("        Time Since Entry:  " + "  ".join(f"p{p}={np.percentile(tse,p):.1f}h" for p in [10, 25, 50, 75, 90]))
    print(f"        'genuine early call' (Current<1.3x Entry AND <1h since entry): {genuine_early} "
          f"({genuine_early/max(len(lateness),1)*100:.1f}%)")

    # (3) chronological traces
    print("\n  (3) full chronological message sequence for sample tokens (E=Entry MC, C=Current MC):")
    samples = [ANSEM_B] + [m for m in ("AWc8uws9", ) for m in timeline if m.startswith("AWc8")][:1]
    # add the first 2 mints with >=4 messages for variety
    multi = [m for m, ms in timeline.items() if len(ms) >= 4][:2]
    for mint in dict.fromkeys([ANSEM_B] + multi):
        if mint not in timeline:
            continue
        print(f"\n    {mint[:10]}..  ({len(timeline[mint])} messages):")
        for d in timeline[mint][:6]:
            e = f"${d['entry']/1e3:.0f}K" if d['entry'] else "  -  "
            c = f"${d['current']/1e3:.0f}K" if d['current'] else "  -  "
            ts = f"{d['tse_h']:.1f}h" if d['tse_h'] is not None else " - "
            pr = f" profit={d['profit']:.0f}x" if d['profit'] else ""
            print(f"        {d['ts']:%Y-%m-%d %H:%M}  {d['type']:10} E={e:>7} C={c:>7}  sinceEntry={ts:>6}{pr}")
    print("=" * 100)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
