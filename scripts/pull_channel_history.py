#!/usr/bin/env python3
"""Pull a Telegram channel's message history into a corpus JSON for the Phase 0.5 backtest.

Reuses the session string generated for chigwell/telegram-mcp (read from
~/telegram-mcp/.env), so no separate login is needed. Read-only: it only iterates
messages. Output matches scripts/backtest_channel.py's corpus format.

    uv run --project ~/telegram-mcp python scripts/pull_channel_history.py @channel --limit 3000
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MCP_ENV = Path.home() / "telegram-mcp" / ".env"


def load_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        sys.exit(f"missing {path} — generate the session string first")
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("channel", help="@username, invite link, or numeric id")
    ap.add_argument("--limit", type=int, default=3000)
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)

    env = load_env(MCP_ENV)
    api_id_s = env.get("TELEGRAM_API_ID", "")
    api_hash = env.get("TELEGRAM_API_HASH", "")
    # Accept TELEGRAM_SESSION_STRING or a labeled TELEGRAM_SESSION_STRING_<LABEL>.
    session = next((v for k, v in env.items()
                    if k.startswith("TELEGRAM_SESSION_STRING") and v), "")
    if not (api_id_s and api_hash):
        return _fail(f"TELEGRAM_API_ID / TELEGRAM_API_HASH not set in {MCP_ENV}")
    if not session:
        return _fail("no TELEGRAM_SESSION_STRING* in the .env — re-run "
                     "session_string_generator.py and answer 'y' to save it")
    api_id = int(api_id_s)

    from telethon.sessions import StringSession
    from telethon.sync import TelegramClient

    client = TelegramClient(StringSession(session), api_id, api_hash)
    client.connect()
    if not client.is_user_authorized():
        return _fail("session not authorized")

    ent = client.get_entity(args.channel)
    title = getattr(ent, "title", args.channel)

    messages: list[dict] = []
    empty = 0
    for m in client.iter_messages(ent, limit=args.limit):
        text = (m.message or "").strip()
        if not text:
            empty += 1
            continue
        ts = m.date.astimezone(timezone.utc) if m.date else None
        messages.append({"id": m.id, "date": int(ts.timestamp()) if ts else 0, "text": text})
    client.disconnect()
    messages.reverse()  # oldest first

    out = Path(args.out) if args.out else (ROOT / "runs" / f"{str(args.channel).lstrip('@')}_corpus.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"channel": args.channel, "title": title, "messages": messages}, indent=2))

    print(f"channel: {title}  ({args.channel})")
    print(f"pulled {len(messages)} text messages ({empty} empty/media skipped) -> {out}")
    if messages:
        lo = datetime.fromtimestamp(messages[0]["date"], tz=timezone.utc)
        hi = datetime.fromtimestamp(messages[-1]["date"], tz=timezone.utc)
        print(f"date range: {lo.isoformat()} .. {hi.isoformat()}")
    return 0


def _fail(msg: str) -> int:
    print(f"ERROR: {msg}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
