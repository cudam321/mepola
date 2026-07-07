"""Alert push delivery invariants — CRIT/WARN reach the operator; a failed send NEVER drops a row."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from memebot.live.notify import (BODY_MAX, THROTTLE_S, _bot_call, format_alert,
                                 pending_rows, push_pass, resolve_chat_id)
from memebot.live.state import LiveState


class FakeSender:
    def __init__(self):
        self.sent = []
        self.fail = False

    async def __call__(self, text):
        if self.fail:
            raise RuntimeError("telegram down")
        self.sent.append(text)


def _pass(send, st, kind_last, now):
    asyncio.run(_pass_async(send, st, kind_last, now))


async def _pass_async(send, st, kind_last, now):
    try:
        await push_pass(send, st, kind_last, now=now)
    except Exception:
        pass                                      # run_alert_push swallows and retries


def test_pushes_crit_and_warn_skips_info_and_throttles_repeats(tmp_path):
    st = LiveState(tmp_path / "s.db")
    st.set_system("alerts_pushed_id", "0")
    st.record_alert(severity="CRIT", kind="ORPHAN_X", message="orphan bag")
    st.record_alert(severity="WARN", kind="RECON_DRIFT", message="drift")
    st.record_alert(severity="INFO", kind="MANUAL_FILL", message="noise")
    send, kind_last = FakeSender(), {}

    _pass(send, st, kind_last, now=1000.0)
    assert len(send.sent) == 2                    # CRIT + WARN; INFO never pages
    assert any("ORPHAN_X" in m and "🚨" in m for m in send.sent)

    st.record_alert(severity="CRIT", kind="ORPHAN_X", message="again")
    _pass(send, st, kind_last, now=1060.0)
    assert len(send.sent) == 2                    # same kind inside the window: page once

    st.record_alert(severity="CRIT", kind="ORPHAN_X", message="still")
    _pass(send, st, kind_last, now=1000.0 + THROTTLE_S + 61)
    assert len(send.sent) == 3                    # window elapsed: page again
    st.close()


def test_failed_send_never_drops_the_alert(tmp_path):
    """AUDIT B4: throttle stamps + high-water advance only on SUCCESS — a Telegram hiccup means
    the row is retried next pass, not consumed."""
    st = LiveState(tmp_path / "s.db")
    st.set_system("alerts_pushed_id", "0")
    st.record_alert(severity="CRIT", kind="WALLET_BOOK_DRIFT", message="wallet != book")
    send, kind_last = FakeSender(), {}
    send.fail = True

    _pass(send, st, kind_last, now=1000.0)
    assert send.sent == []
    assert st.get_system("alerts_pushed_id") == "0"    # NOT consumed
    assert kind_last == {}                             # NOT stamped as delivered

    send.fail = False
    _pass(send, st, kind_last, now=1020.0)             # 20s later — would be throttled if stamped
    assert len(send.sent) == 1 and "WALLET_BOOK_DRIFT" in send.sent[0]
    assert st.get_system("alerts_pushed_id") == "1"
    st.close()


def test_format_alert_truncates_a_program_log_dump():
    """A 7KB RPC simulation dump is a log line, not a page — the body is hard-capped."""
    msg = format_alert({"severity": "WARN", "kind": "ALGO_ORDER_FAILED",
                        "message": "x" * 8000, "ts": "2026-07-07T07:43:52+00:00"})
    assert len(msg) < BODY_MAX + 120
    assert "[truncated]" in msg
    assert msg.endswith("2026-07-07T07:43:52Z")


def _bot_transport(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=5.0)


def test_bot_errors_never_leak_the_token():
    """The Bot API URL embeds the token; a failure must surface status-only text."""
    def handler(request):
        return httpx.Response(401, json={"ok": False, "description": "Unauthorized"})

    async def go():
        async with _bot_transport(handler) as http:
            with pytest.raises(RuntimeError) as ei:
                await _bot_call(http, "SECRET-TOKEN", "sendMessage", {"chat_id": 1, "text": "x"})
            assert "SECRET-TOKEN" not in str(ei.value)
            assert "401" in str(ei.value)
    asyncio.run(go())


def test_resolve_chat_id_prefers_env_then_cache_then_getupdates(tmp_path, monkeypatch):
    st = LiveState(tmp_path / "s.db")

    def handler(request):
        return httpx.Response(200, json={"ok": True, "result": [
            {"message": {"chat": {"id": 111, "type": "private"}}},
            {"message": {"chat": {"id": 222, "type": "private"}}},   # newest PRIVATE wins
            {"message": {"chat": {"id": -333, "type": "group"}}},    # groups never capture alerts
        ]})

    async def go():
        async with _bot_transport(handler) as http:
            monkeypatch.setenv("TELEGRAM_ALERT_CHAT_ID", "999")
            assert await resolve_chat_id(http, "t", st) == 999
            monkeypatch.delenv("TELEGRAM_ALERT_CHAT_ID")
            assert await resolve_chat_id(http, "t", st) == 222      # discovered
            assert st.get_system("alert_bot_chat_id") == "222"      # cached
            assert await resolve_chat_id(http, "t", st) == 222      # cache hit
    asyncio.run(go())
    st.close()


def test_hello_sends_even_when_the_sender_is_acquired_late(tmp_path, monkeypatch):
    """Production bug (2026-07-07): the operator messaged the bot AFTER the deploy, the chat id
    resolved 3 minutes into the run — and the init-only hello never sent. The hello must fire
    whenever a sender is (re)acquired."""
    from memebot.live import notify
    st = LiveState(tmp_path / "s.db")
    st.set_system("alerts_pushed_id", "0")
    monkeypatch.setattr(notify, "POLL_S", 0.01)
    sent = []

    async def flaky_pick(client, state, http):
        flaky_pick.calls += 1
        if flaky_pick.calls < 3:                     # chat id not resolvable yet
            return None, "none"

        async def send(text):
            sent.append(text)
        return send, "bot"
    flaky_pick.calls = 0
    monkeypatch.setattr(notify, "_pick_sender", flaky_pick)

    async def go():
        task = asyncio.get_event_loop().create_task(notify.run_alert_push(None, st))
        for _ in range(200):
            await asyncio.sleep(0.01)
            if sent:
                break
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    asyncio.run(go())
    assert sent and "notifier online" in sent[0]     # hello arrived despite the late acquire
    assert st.get_system("alerts_push_hello_bot") == "done"
    st.close()


def test_failed_sender_is_dropped_and_repicked(tmp_path, monkeypatch):
    """A rotated bot token must not leave the loop retrying a dead closure forever — on a send
    failure the channel is re-picked (env + cached chat id re-read) next pass."""
    from memebot.live import notify
    st = LiveState(tmp_path / "s.db")
    st.set_system("alerts_pushed_id", "0")
    st.set_system("alerts_push_hello_bot", "done")   # hello already proven
    st.record_alert(severity="CRIT", kind="X", message="page me")
    monkeypatch.setattr(notify, "POLL_S", 0.01)
    sent = []

    async def rotating_pick(client, state, http):
        rotating_pick.calls += 1

        async def dead(text):
            raise RuntimeError("401 Unauthorized (old token)")

        async def alive(text):
            sent.append(text)
        return (dead if rotating_pick.calls == 1 else alive), "bot"
    rotating_pick.calls = 0
    monkeypatch.setattr(notify, "_pick_sender", rotating_pick)

    async def go():
        task = asyncio.get_event_loop().create_task(notify.run_alert_push(None, st))
        for _ in range(200):
            await asyncio.sleep(0.01)
            if sent:
                break
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    asyncio.run(go())
    assert rotating_pick.calls >= 2                  # dead sender was dropped and re-picked
    assert any("page me" in m for m in sent)         # the CRIT was never lost
    st.close()
