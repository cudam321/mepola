"""Real-time Telegram listener — @your_channel NewMessage -> parsed first-call BUY Signals.

Reuses the exact parse path (`ingest.telegram_mcp.signals_from_messages`) and the session loading
of `scripts/pull_channel_history.py` (the chigwell/telegram-mcp session string from
~/telegram-mcp/.env, or a TELEGRAM_SESSION_STRING_* in the project .env). Read-only usage.

`telethon` is an optional dependency (extra `prod-ingest`); it is imported lazily so this module
imports fine without it (the engine/tests never need it).
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, Optional

from memebot.ingest.telegram_mcp import signals_from_messages
from memebot.live.state import utcnow
from memebot.models import Signal

log = logging.getLogger("memebot.live.listener")

MCP_ENV = Path.home() / "telegram-mcp" / ".env"
PROJECT_ENV = Path(__file__).resolve().parents[3] / ".env"


def _load_env_file(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def _resolve_credentials() -> tuple[int, str, str]:
    """(api_id, api_hash, session_string). Checks process env, then telegram-mcp .env, then project .env."""
    merged = {**_load_env_file(PROJECT_ENV), **_load_env_file(MCP_ENV), **os.environ}
    api_id = merged.get("TELEGRAM_API_ID", "")
    api_hash = merged.get("TELEGRAM_API_HASH", "")
    session = next((v for k, v in merged.items() if k.startswith("TELEGRAM_SESSION_STRING") and v), "")
    if not (api_id and api_hash and session):
        raise RuntimeError("missing TELEGRAM_API_ID / TELEGRAM_API_HASH / TELEGRAM_SESSION_STRING* "
                           "(check ~/telegram-mcp/.env or the project .env)")
    return int(api_id), api_hash, session


def build_client():
    """Construct an async Telethon client from the stored session (lazy telethon import)."""
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    api_id, api_hash, session = _resolve_credentials()
    return TelegramClient(StringSession(session), api_id, api_hash)


def _mark_listener_ok(state) -> None:
    if state is None:
        return
    try:
        state.set_system("last_listener_ok_ts", utcnow().isoformat())
    except Exception:
        pass


async def _process_event(ev, channel: str, on_call, state) -> None:
    """Parse ONE Telegram message event -> forward tradable first-call BUYs, and advance the
    persisted high-water message id so downtime catch-up knows where to resume. Pure of
    telethon (duck-typed `ev`) so it is unit-testable without the optional dep.

    M14 (audit 2026-07-07): the high-water advances only AFTER ingest succeeds — consuming a
    message whose on_call raised would silently drop the call (invisible EV loss); leaving it
    behind the watermark means the next catch-up pass retries it (first-call dedup makes any
    overlap a no-op)."""
    text = (getattr(ev, "raw_text", "") or "").strip()
    mid = getattr(ev, "id", None)
    if state is not None:
        try:
            state.set_system("last_listener_ok_ts", utcnow().isoformat())
        except Exception:
            pass
    if text:
        ev_date = getattr(ev, "date", None)
        ts = ev_date.astimezone(timezone.utc) if ev_date else datetime.now(timezone.utc)
        sigs = signals_from_messages(
            [{"id": mid, "date": int(ts.timestamp()), "text": text}], channel)
        for sig in sigs:
            if sig.is_tradable:
                await on_call(sig)     # a raise = message NOT consumed (retried by catch-up)
    if state is not None and mid:
        try:
            prev = int(state.get_system("last_listener_msg_id") or 0)
            if int(mid) > prev:
                state.set_system("last_listener_msg_id", str(int(mid)))
        except Exception:
            pass


async def _catch_up(client, channel: str, on_call, state) -> None:
    """Replay messages posted while we were disconnected (id > high-water), oldest-first, so
    a restart/outage does not silently drop calls. The first-ever connect just pins the
    high-water to the latest message (no full-history replay). `on_call`'s own first-call
    dedup makes any overlap a no-op. Best-effort: a failure here never blocks going live."""
    if state is None:
        return
    try:
        last_id = int(state.get_system("last_listener_msg_id") or 0)
    except (TypeError, ValueError):
        last_id = 0
    try:
        if last_id <= 0:
            latest = list(await client.get_messages(channel, limit=1))
            if latest:
                state.set_system("last_listener_msg_id", str(int(latest[0].id)))
            return
        # M14: paginate OLDEST-FIRST until fully caught up — a single newest-first page of 200
        # silently dropped everything older after a long outage, and the high-water then jumped
        # past the dropped calls forever.
        total = 0
        while True:
            missed = list(await client.get_messages(channel, min_id=last_id, limit=200,
                                                    reverse=True))    # oldest-first from min_id
            if not missed:
                break
            for ev in missed:
                await _process_event(ev, channel, on_call, state)
            total += len(missed)
            last_id = max(int(getattr(m, "id", 0) or 0) for m in missed)
            if len(missed) < 200:
                break
        if total:
            log.info("listener catch-up: processed %d missed message(s)", total)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("listener catch-up failed")
        # re-audit: a message that deterministically raises would wedge the watermark FOREVER
        # (every later missed call silently lost) — after repeated failures at the same
        # high-water, page the operator instead of dying softly (H7 class).
        try:
            key = f"catchup_fail_{last_id}"
            n = int(state.get_system(key) or 0) + 1
            state.set_system(key, str(n))
            if n == 5:
                state.record_alert(
                    severity="CRIT", kind="CATCHUP_WEDGED",
                    message=f"listener catch-up has failed {n}x at message id {last_id} — "
                            "downtime replay is stuck (poison message?); calls after it are "
                            "not being recovered")
        except Exception:
            pass


async def _listener_heartbeat(client, channel: str, on_call, state, *,
                              beat_s: float = 45.0, poll_s: float = 300.0) -> None:
    """Audit #9: while connected, refresh the listener liveness stamp (so a wedged coroutine is
    DETECTABLE by Monitor.check_listener), and periodically re-run catch-up — which polls the channel
    and processes any messages the push handler silently missed (the connected-but-deaf failure mode),
    SELF-HEALING it. `on_call`'s first-call dedup makes the replay a no-op when the push loop saw them."""
    since_poll = 0.0
    while True:
        await asyncio.sleep(beat_s)
        try:
            if client.is_connected():
                _mark_listener_ok(state)
            since_poll += beat_s
            if since_poll >= poll_s:
                since_poll = 0.0
                await _catch_up(client, channel, on_call, state)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("listener heartbeat pass failed")


async def run_listener(channel: str, on_call: Callable[[Signal], Awaitable[None]],
                       *, client=None, state=None, max_backoff: float = 300.0,
                       on_client: Optional[Callable] = None,
                       _stop_after_disconnect: bool = False) -> None:
    """Connect and forward each tradable first-call BUY to `on_call`, FOREVER. On any
    disconnect or error, reconnect with capped exponential backoff — the exception must
    never reach the orchestrator's gather (crash-restart on a transient Telegram blip) and
    the coroutine must never return silently while the other loops keep running (a signal
    bot that goes deaf is invisibly EV-negative). On each (re)connect, replay calls missed
    during downtime from the persisted high-water id."""
    from telethon import events

    client = client or build_client()

    @client.on(events.NewMessage(chats=[channel]))
    async def _handler(ev):
        await _process_event(ev, channel, on_call, state)

    backoff = 2.0
    while True:
        try:
            await client.start()
            _mark_listener_ok(state)
            if on_client is not None:
                try:      # hand the CONNECTED client to the caller (the alert notifier reuses it)
                    on_client(client)
                except Exception:
                    log.exception("on_client callback failed (listener unaffected)")
            await _catch_up(client, channel, on_call, state)
            backoff = 2.0                       # a clean connect resets the backoff
            hb = asyncio.create_task(_listener_heartbeat(client, channel, on_call, state))
            try:
                await client.run_until_disconnected()
            finally:
                hb.cancel()
                try:
                    await hb
                except (asyncio.CancelledError, Exception):
                    pass                        # heartbeat is best-effort; never block reconnect
            log.warning("listener disconnected; reconnecting")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("listener error; reconnecting in %.0fs", backoff)
            if state is not None:
                try:
                    state.record_alert(severity="WARN", kind="LISTENER_RECONNECT",
                                       message=f"listener error; retrying in {backoff:.0f}s")
                except Exception:
                    pass
        if _stop_after_disconnect:
            return
        await asyncio.sleep(backoff)
        backoff = min(max_backoff, backoff * 2)
