"""Telegram ingestion adapter.

This bridges raw Telegram messages into parsed ``Signal`` objects. It is the seam between
two interchangeable sources of the SAME message shape:

  * DEV / BACKTEST — the `chigwell/telegram-mcp` server. You (the agent) call its message
    tools (e.g. list/read a channel's history) and pass the returned message dicts to
    ``signals_from_messages``. Each message dict is expected to expose at least an id, a
    date (epoch seconds, ms, or ISO string), and the text. Field names vary slightly by
    tool, so ``_get`` checks a few common aliases (text/message/content, date/timestamp).

  * PRODUCTION — a standalone Telethon user-session service. The handler receives
    ``events.NewMessage`` and builds the same dict ``{"id", "date", "text"}``, so the
    exact parsing path is shared and nothing downstream changes. Sketch:

        from telethon import TelegramClient, events
        client = TelegramClient(session, api_id, api_hash)
        @client.on(events.NewMessage(chats=channels))
        async def handler(ev):
            sig = signals_from_messages([{ "id": ev.id, "date": ev.date, "text": ev.raw_text }], channel)[0]
            ...  # enrich -> safety gate -> sim/execute

History pull for the Phase 0.5 backtest is therefore: get messages via the MCP ->
``signals_from_messages`` -> ``save_corpus_json`` -> run ``scripts/backtest_channel.py``.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from memebot.models import Signal, SignalSide
from memebot.parser.signal_parser import parse_message


def _coerce_dt(value: Any) -> datetime:
    """Epoch seconds/ms or ISO-8601 string -> tz-aware UTC datetime."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        secs = float(value)
        if secs > 1e12:  # milliseconds
            secs /= 1000.0
        return datetime.fromtimestamp(secs, tz=timezone.utc)
    if isinstance(value, str):
        s = value.strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    raise ValueError(f"cannot coerce {value!r} to datetime")


def _get(msg: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for k in keys:
        if k in msg and msg[k] is not None:
            return msg[k]
    return default


def signals_from_messages(messages: Iterable[dict[str, Any]], source_channel: str) -> list[Signal]:
    """Parse raw Telegram message dicts into Signals (one per message)."""
    out: list[Signal] = []
    for msg in messages:
        text = _get(msg, "text", "message", "content", "raw_text", default="") or ""
        posted = _coerce_dt(_get(msg, "date", "timestamp", "posted_at", default=0))
        mid = int(_get(msg, "id", "message_id", default=0) or 0)
        sig = parse_message(source_channel, mid, posted, text)
        # Honor explicit overrides if the source already structured the call.
        if msg.get("mint"):
            sig.mint = msg["mint"]
            sig.parse_confidence = max(sig.parse_confidence, 1.0)
        if msg.get("side"):
            try:
                sig.side = SignalSide(str(msg["side"]).lower())
            except ValueError:
                pass
        out.append(sig)
    return out


def load_corpus_json(path: str | Path) -> list[Signal]:
    """Load a saved corpus. Accepts a JSON list of message records (id/date/text[,mint,side])."""
    records = json.loads(Path(path).read_text())
    if isinstance(records, dict) and "messages" in records:
        channel = records.get("channel", "corpus")
        records = records["messages"]
    else:
        channel = "corpus"
    # Allow each record to carry its own channel.
    grouped: dict[str, list[dict]] = {}
    for r in records:
        grouped.setdefault(r.get("source_channel", channel), []).append(r)
    signals: list[Signal] = []
    for ch, recs in grouped.items():
        signals.extend(signals_from_messages(recs, ch))
    return signals


def save_corpus_json(signals: list[Signal], path: str | Path) -> None:
    Path(path).write_text(json.dumps([s.to_dict() for s in signals], indent=2))


def first_call_per_mint(signals: list[Signal]) -> list[Signal]:
    """The earliest tradable (BUY + mint) signal for each token, oldest first.

    Channels repeat a token many times (fresh call, then BUY-MORE / HOLDING / PROFIT
    follow-ups). The unit for measuring channel edge is the FIRST actionable call per
    token — counting every follow-up as a separate trade massively double-counts.
    """
    first: dict[str, Signal] = {}
    for s in sorted(signals, key=lambda x: x.posted_at):
        if s.is_tradable and s.mint not in first:
            first[s.mint] = s
    return sorted(first.values(), key=lambda x: x.posted_at)
