"""CRIT/WARN alerts → the operator's phone, via a Telegram BOT (or Saved Messages fallback).

Detection without delivery is worthless: the ORPHAN_BALANCE CRIT fired every 6 minutes for
5 hours (the phantom-stop incident) before a human saw it.

Delivery channel (2026-07-07 v2): Saved Messages is structurally SILENT — Telegram never
notifies you of your OWN outgoing messages, and the alerts are sent from the operator's own
session. A dedicated bot is a different sender, so its messages actually ring the phone.
  - `TELEGRAM_ALERT_BOT_TOKEN` set → push via the Bot HTTP API. The chat id comes from
    `TELEGRAM_ALERT_CHAT_ID`, else the cached `alert_bot_chat_id` system row, else it is
    auto-resolved from the bot's getUpdates (the operator just sends the bot any message once).
  - No token → fall back to Saved Messages over the listener's connected client (visible in
    Telegram, but silent — the hello message says so).
Bot mode needs no telethon client at all, so it is started at boot as a supervised task and
survives listener outages. Fail-soft by design: a Telegram hiccup can never affect trading.

SECURITY: the Bot API URL embeds the token — httpx exceptions embed the URL — so every bot
HTTP call is caught and re-raised as a status-only RuntimeError (same rule as jupiter_swap).

Delivery invariant (AUDIT B4, 2026-07-07): the throttle stamp AND the high-water advance
happen only AFTER a send succeeds, per message, in id order — a failed send stops the pass
at that row, so it (and everything behind it) is retried next pass. Stamping on the ATTEMPT
would make the retry look throttled and silently consume the alert.

Spam control: one push per alert KIND per THROTTLE_S (the orphan alert repeats every 6 min —
the operator needs ONE page, not eighty). A row skipped as throttled advances the high-water:
its kind was successfully delivered within the window, so consuming it is safe. Alert bodies
are hard-truncated: a raw RPC simulation dump is a log line, not a page.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Awaitable, Callable, Optional

import httpx

log = logging.getLogger("memebot.live.notify")

THROTTLE_S = 1800.0     # one push per alert kind per 30 min
POLL_S = 20.0           # alerts-table poll cadence
BODY_MAX = 500          # a page is a headline, not a stack trace

BOT_API = "https://api.telegram.org"


def _short(text: str, limit: int = BODY_MAX) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit].rstrip() + " … [truncated]"


def format_alert(r: dict) -> str:
    icon = "🚨" if r["severity"] == "CRIT" else "⚠️"
    return (f"{icon} mepola {r['severity']} — {r['kind']}\n"
            f"{_short(r['message'])}\n{(r['ts'] or '')[:19]}Z")


def pending_rows(state, last_id: int) -> list[dict]:
    """New CRIT/WARN alert rows past the high-water, oldest first (pure read)."""
    return state.query(
        "SELECT id, ts, severity, kind, message FROM alerts "
        "WHERE id > ? AND severity IN ('CRIT','WARN') ORDER BY id LIMIT 30", (last_id,))


# -- bot transport (token never logged; errors are status-only) ------------- #

async def _bot_call(http: httpx.AsyncClient, token: str, method: str, payload: dict) -> dict:
    try:
        r = await http.post(f"{BOT_API}/bot{token}/{method}", json=payload)
        body = r.json()
    except httpx.HTTPError as e:
        raise RuntimeError(f"bot {method} transport error: {type(e).__name__}") from None
    except ValueError:
        raise RuntimeError(f"bot {method} returned non-JSON (HTTP {r.status_code})") from None
    if not body.get("ok"):
        # description is Telegram's own text ("chat not found", ...) — safe, no token
        raise RuntimeError(f"bot {method} failed (HTTP {r.status_code}): "
                           f"{_short(str(body.get('description')), 120)}")
    return body.get("result") or {}


async def resolve_chat_id(http: httpx.AsyncClient, token: str, state) -> Optional[int]:
    """Where should the bot deliver? Env pin > cached resolution > getUpdates discovery
    (the operator messages the bot once; we take the most recent private chat id and cache it)."""
    env = (os.environ.get("TELEGRAM_ALERT_CHAT_ID") or "").strip()
    if env:
        try:
            return int(env)
        except ValueError:
            log.error("TELEGRAM_ALERT_CHAT_ID is not an integer — ignoring")
    cached = state.get_system("alert_bot_chat_id")
    if cached:
        try:
            return int(cached)
        except ValueError:
            pass
    try:
        updates = await _bot_call(http, token, "getUpdates", {"limit": 100})
    except Exception as e:
        log.warning("bot getUpdates failed: %s", e)
        return None
    chat_id = None
    for u in updates if isinstance(updates, list) else []:
        chat = ((u.get("message") or u.get("edited_message") or {}).get("chat") or {})
        # PRIVATE chats only (re-audit): a group the bot was added to — or a stranger who found
        # the bot before the operator messaged it — must not capture the alert stream
        if chat.get("type") == "private" and chat.get("id") is not None:
            chat_id = int(chat["id"])           # newest wins
    if chat_id is not None:
        state.set_system("alert_bot_chat_id", str(chat_id))
        log.info("alert bot chat id resolved and cached: %s (verify the hello message "
                 "reached YOUR chat)", chat_id)
    return chat_id


def _make_bot_sender(http: httpx.AsyncClient, token: str, chat_id: int) -> Callable[[str], Awaitable[None]]:
    async def send(text: str) -> None:
        await _bot_call(http, token, "sendMessage", {"chat_id": chat_id, "text": text})
    return send


def _make_me_sender(client) -> Callable[[str], Awaitable[None]]:
    async def send(text: str) -> None:
        await client.send_message("me", text)
    return send


# -- delivery loop ----------------------------------------------------------- #

async def push_pass(send: Callable[[str], Awaitable[None]], state,
                    kind_last: dict[str, float], *, now: float) -> None:
    """One delivery pass. Advances alerts_pushed_id row-by-row: throttled rows are consumed
    immediately; a row needing a send is consumed (and its kind stamped) only after the send
    succeeds. A send failure raises out — nothing past the failed row is consumed."""
    last = int(state.get_system("alerts_pushed_id") or "0")
    progress = last
    try:
        for r in pending_rows(state, last):
            if now - kind_last.get(r["kind"], -THROTTLE_S) < THROTTLE_S:
                progress = r["id"]          # kind already delivered this window — consume
                continue
            await send(format_alert(r))
            kind_last[r["kind"]] = now      # stamp ONLY on success (B4)
            progress = r["id"]
    finally:
        if progress != last:
            state.set_system("alerts_pushed_id", str(progress))


async def _pick_sender(client, state, http: httpx.AsyncClient
                       ) -> tuple[Callable[[str], Awaitable[None]], str]:
    """(send, mode). Prefer the bot (it actually notifies); fall back to Saved Messages."""
    token = (os.environ.get("TELEGRAM_ALERT_BOT_TOKEN") or "").strip()
    if token:
        chat_id = await resolve_chat_id(http, token, state)
        if chat_id is not None:
            return _make_bot_sender(http, token, chat_id), "bot"
        log.warning("TELEGRAM_ALERT_BOT_TOKEN set but no chat id — send the bot any "
                    "message once (or set TELEGRAM_ALERT_CHAT_ID); falling back")
    if client is not None:
        return _make_me_sender(client), "me"
    return None, "none"


async def _hello_once(send, mode: str, state) -> None:
    """One-time end-to-end delivery proof PER CHANNEL — sent whenever a sender is (re)acquired,
    not just at init, so a chat id that resolves minutes after boot (the operator messages the
    bot AFTER the deploy) still gets its proof. Keyed per mode so wiring the bot re-proves it."""
    hello_key = f"alerts_push_hello_{mode}"
    if send is None or state.get_system(hello_key) is not None:
        return
    extra = ("" if mode == "bot" else
             "\n(note: Saved Messages never ring — set TELEGRAM_ALERT_BOT_TOKEN "
             "for real push notifications)")
    await send("🟢 mepola alert notifier online — CRIT/WARN alerts land here" + extra)
    state.set_system(hello_key, "done")


async def run_alert_push(client, state) -> None:
    """Poll the alerts table forever; push new CRIT/WARN to the operator. Never raises out
    (a dead notifier must not take its host task down). `client` may be None in bot mode."""
    async with httpx.AsyncClient(timeout=15.0) as http:
        try:
            if state.get_system("alerts_pushed_id") is None:
                # first boot: start from the current high-water so history is never replayed
                m = state.query("SELECT COALESCE(MAX(id),0) AS m FROM alerts")[0]["m"]
                state.set_system("alerts_pushed_id", str(m))
        except Exception:
            log.exception("notifier init failed (pushes will still be attempted)")
        send, mode = None, "none"
        kind_last: dict[str, float] = {}
        while True:
            try:
                if send is None:        # keep trying until a channel becomes reachable
                    send, mode = await _pick_sender(client, state, http)
                if send is not None:
                    await _hello_once(send, mode, state)
                    await push_pass(send, state, kind_last, now=time.monotonic())
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("alert push pass failed (unsent rows retry next pass)")
                # drop the sender and re-pick next pass: a ROTATED bot token (or a died client)
                # must not leave the loop retrying a dead closure forever — env and the cached
                # chat id are re-read on re-pick, so rotation heals without a restart
                send, mode = None, "none"
            await asyncio.sleep(POLL_S)
