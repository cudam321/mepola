"""Listener resilience — the reconnect/catch-up helpers (F30), tested without telethon.

`run_listener`'s outer loop is thin and telethon-coupled (imported lazily), so it is not
exercised here; its behaviour is composed of these pure, duck-typed helpers:
_process_event (parse + high-water advance) and _catch_up (downtime replay)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from memebot.live import listener


class FakeState:
    def __init__(self):
        self.kv: dict[str, str] = {}
        self.alerts: list[dict] = []

    def get_system(self, k, default=None):
        return self.kv.get(k, default)

    def set_system(self, k, v, **kw):
        self.kv[k] = v

    def record_alert(self, **kw):
        self.alerts.append(kw)


def _ev(mid, text="", date=None):
    return SimpleNamespace(id=mid, raw_text=text, date=date)


def test_process_event_advances_highwater_and_ignores_empty():
    st = FakeState()
    calls = []
    asyncio.run(listener._process_event(_ev(10, ""), "@c", lambda s: calls.append(s), st))
    assert st.kv["last_listener_msg_id"] == "10"
    assert "last_listener_ok_ts" in st.kv
    assert calls == []                              # empty text -> nothing forwarded


def test_process_event_highwater_is_monotonic():
    st = FakeState()
    st.kv["last_listener_msg_id"] = "20"
    asyncio.run(listener._process_event(_ev(15, ""), "@c", lambda s: None, st))
    assert st.kv["last_listener_msg_id"] == "20"    # an older id never lowers the high-water


def test_process_event_forwards_tradable(monkeypatch):
    st = FakeState()
    calls = []
    monkeypatch.setattr(listener, "signals_from_messages",
                        lambda msgs, ch: [SimpleNamespace(is_tradable=True)])

    async def on_call(sig):
        calls.append(sig)

    asyncio.run(listener._process_event(_ev(5, "buy X"), "@c", on_call, st))
    assert len(calls) == 1


class FakeClient:
    """Duck-typed telethon client: get_messages honors min_id/limit/reverse (reverse=True ->
    oldest-first from min_id, like telethon)."""

    def __init__(self, msgs):
        self._msgs = msgs

    async def get_messages(self, channel, *, limit=None, min_id=None, reverse=False):
        out = self._msgs
        if min_id is not None:
            out = [m for m in out if m.id > min_id]
        out = sorted(out, key=lambda m: m.id, reverse=not reverse)
        return out[:limit] if limit else out


def test_catch_up_first_connect_pins_highwater_without_replay():
    st = FakeState()
    calls = []
    client = FakeClient([_ev(1, "a"), _ev(2, "b"), _ev(3, "c")])
    asyncio.run(listener._catch_up(client, "@c", lambda s: calls.append(s), st))
    assert st.kv["last_listener_msg_id"] == "3"     # pinned to latest
    assert calls == []                              # NO full-history replay on first connect


def test_catch_up_replays_missed_oldest_first(monkeypatch):
    st = FakeState()
    st.kv["last_listener_msg_id"] = "1"
    seen = []
    monkeypatch.setattr(listener, "signals_from_messages",
                        lambda msgs, ch: [SimpleNamespace(is_tradable=True, _id=msgs[0]["id"])])

    async def on_call(sig):
        seen.append(sig._id)

    client = FakeClient([_ev(1, "a"), _ev(2, "b"), _ev(3, "c")])
    asyncio.run(listener._catch_up(client, "@c", on_call, st))
    assert seen == [2, 3]                            # missed messages, oldest-first
    assert st.kv["last_listener_msg_id"] == "3"      # high-water advanced past them


def test_catch_up_noop_without_state():
    # no state -> nothing to persist, no crash
    asyncio.run(listener._catch_up(FakeClient([]), "@c", lambda s: None, None))


def test_catch_up_paginates_past_the_page_limit(monkeypatch):
    """M14: >1 page of missed messages must ALL replay (oldest were silently dropped before)."""
    st = FakeState()
    st.kv["last_listener_msg_id"] = "0"
    seen = []
    monkeypatch.setattr(listener, "signals_from_messages",
                        lambda msgs, ch: [SimpleNamespace(is_tradable=True, _id=msgs[0]["id"])])

    async def on_call(sig):
        seen.append(sig._id)

    client = FakeClient([_ev(i, f"m{i}") for i in range(1, 451)])   # 450 missed > 2 pages
    st.kv["last_listener_msg_id"] = "0"
    # first-connect pin would skip; force the replay path
    asyncio.run(listener._catch_up(client, "@c", on_call, st))
    # last_id==0 -> first-connect pin path; set a real high-water and rerun
    st2 = FakeState(); st2.kv["last_listener_msg_id"] = "50"
    seen.clear()
    asyncio.run(listener._catch_up(client, "@c", on_call, st2))
    assert seen == list(range(51, 451))                  # every missed message, oldest-first
    assert st2.kv["last_listener_msg_id"] == "450"


def test_failed_ingest_does_not_consume_the_message(monkeypatch):
    """M14: high-water advances only after on_call succeeds — a crash mid-ingest retries."""
    st = FakeState()
    monkeypatch.setattr(listener, "signals_from_messages",
                        lambda msgs, ch: [SimpleNamespace(is_tradable=True)])

    async def boom(sig):
        raise RuntimeError("ingest died")

    try:
        asyncio.run(listener._process_event(_ev(7, "buy X"), "@c", boom, st))
    except RuntimeError:
        pass
    assert st.kv.get("last_listener_msg_id") is None      # NOT consumed
