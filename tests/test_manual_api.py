"""M2–M5 — the manual trading API (validation + persistence; offline).

These routes only WRITE orders/watchlist/controller — the engine executes them. So the tests assert
validation, gating feedback, and that the right rows land."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import dashboard.server.app as appmod  # noqa: E402
from memebot.live.state import LiveState  # noqa: E402

MINT = "MintManuaAPK22222222222222222222222222222222"   # valid base58 (no 0/O/I/l) — audit #35
NOW = datetime.now(timezone.utc)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db = tmp_path / "live_state.db"
    LiveState(db).close()
    monkeypatch.setattr(appmod, "DB_PATH", str(db))
    with TestClient(appmod.app) as c:
        yield c, str(db)


def _mk_manual_position(db, mint=MINT, ticker="TOK"):
    st = LiveState(db)
    pid = st.create_position(mint=mint, ticker=ticker, signal_at=NOW, signal_price=1.0,
                             state="ENTERED")
    st.update_position(mint, controller="manual", entry_price=1.0, stake_usd=5.0, tokens_qty=5.0,
                       remaining_frac=1.0, current_price=1.0, current_multiple=1.0)
    st.close()
    return pid


def test_market_buy_order_created(env):
    c, db = env
    r = c.post("/api/manual/order", json={"mint": MINT, "side": "buy", "kind": "market",
                                          "size_kind": "usd", "size_value": 4.0, "ticker": "TOK"})
    assert r.status_code == 200, r.text
    oid = r.json()["id"]
    st = LiveState(db)
    o = st.get_order(oid); st.close()
    assert o["kind"] == "market" and o["trigger_type"] == "now" and o["status"] == "open"
    assert o["size_value"] == 4.0 and o["expires_at"] is not None      # market gets a short expiry


def test_buy_clamped_to_hard_cap(env):
    c, db = env
    st = LiveState(db); st.set_system("manual_trade_hard_cap_usd", "10"); st.close()
    r = c.post("/api/manual/order", json={"mint": MINT, "side": "buy", "kind": "market",
                                          "size_kind": "usd", "size_value": 999.0})
    assert r.status_code == 200 and r.json()["size_value"] == 10.0


def test_buy_invalid_mint_rejected(env):
    c, _ = env
    r = c.post("/api/manual/order", json={"mint": "short", "side": "buy", "kind": "market",
                                          "size_kind": "usd", "size_value": 4.0})
    assert r.status_code == 422


def test_buy_blocked_when_already_holding(env):
    c, db = env
    st = LiveState(db)
    st.create_position(mint=MINT, ticker="TOK", signal_at=NOW, signal_price=1.0, state="ENTERED")
    st.update_position(MINT, controller="algo", entry_price=1.0)
    st.close()
    r = c.post("/api/manual/order", json={"mint": MINT, "side": "buy", "kind": "market",
                                          "size_kind": "usd", "size_value": 4.0})
    assert r.status_code == 409 and "already holding" in r.json()["error"]


def test_direct_buys_disabled_when_cap_zero(env):
    c, db = env
    st = LiveState(db); st.set_system("manual_cap_usd", "0"); st.close()
    r = c.post("/api/manual/order", json={"mint": MINT, "side": "buy", "kind": "market",
                                          "size_kind": "usd", "size_value": 4.0})
    assert r.status_code == 409 and "disabled" in r.json()["error"].lower()


def test_market_buy_blocked_by_kill_switch(env):
    c, db = env
    st = LiveState(db); st.set_system("kill_switch", "on"); st.close()
    r = c.post("/api/manual/order", json={"mint": MINT, "side": "buy", "kind": "market",
                                          "size_kind": "usd", "size_value": 4.0})
    assert r.status_code == 409 and "kill" in r.json()["error"].lower()


def test_sell_requires_a_position(env):
    c, _ = env
    r = c.post("/api/manual/order", json={"mint": MINT, "side": "sell", "kind": "market",
                                          "size_kind": "token_frac", "size_value": 1.0})
    assert r.status_code == 409 and "no open position" in r.json()["error"]


def test_sell_order_on_algo_position_takes_it_over(env):
    # setting a manual exit on an algo position implicitly takes it over (controller -> manual)
    c, db = env
    st = LiveState(db)
    st.create_position(mint=MINT, ticker="TOK", signal_at=NOW, signal_price=1.0, state="ENTERED")
    st.update_position(MINT, controller="algo", entry_price=1.0, tokens_qty=5.0, remaining_frac=1.0)
    st.close()
    r = c.post("/api/manual/order", json={"mint": MINT, "side": "sell", "kind": "take_profit",
                                          "trigger_type": "mult_at_or_above", "trigger_value": 3.0,
                                          "size_kind": "token_frac", "size_value": 0.5})
    assert r.status_code == 200, r.text
    st = LiveState(db)
    assert st.get_position(MINT)["controller"] == "manual"      # taken over at set time
    assert int(st.get_system("controller_rev")) >= 1
    st.close()


def test_take_profit_order_on_manual_position(env):
    c, db = env
    _mk_manual_position(db)
    r = c.post("/api/manual/order", json={"mint": MINT, "side": "sell", "kind": "take_profit",
                                          "trigger_type": "mult_at_or_above", "trigger_value": 3.0,
                                          "size_kind": "token_frac", "size_value": 0.5})
    assert r.status_code == 200, r.text
    assert len(c.get("/api/manual/orders").json()["orders"]) == 1


def test_cancel_and_modify_order(env):
    c, db = env
    _mk_manual_position(db)
    oid = c.post("/api/manual/order", json={"mint": MINT, "side": "sell", "kind": "stop_loss",
                                            "trigger_type": "price_at_or_below", "trigger_value": 0.7,
                                            "size_kind": "token_frac", "size_value": 1.0}).json()["id"]
    r = c.patch(f"/api/manual/order/{oid}", json={"trigger_value": 0.8})
    assert r.status_code == 200
    st = LiveState(db); assert st.get_order(oid)["trigger_value"] == 0.8; st.close()
    assert c.delete(f"/api/manual/order/{oid}").status_code == 200
    st = LiveState(db); assert st.get_order(oid)["status"] == "cancelled"; st.close()
    assert c.delete(f"/api/manual/order/{oid}").status_code == 409   # already cancelled


def test_watchlist_add_and_remove(env):
    c, db = env
    assert c.post("/api/watchlist", json={"mint": MINT, "ticker": "TOK"}).status_code == 200
    st = LiveState(db); assert st.is_watched(MINT); st.close()
    assert c.delete(f"/api/watchlist/{MINT}").status_code == 200
    st = LiveState(db); assert not st.is_watched(MINT); st.close()


def test_non_finite_numbers_rejected(env):
    # review H2: Infinity/NaN must not pass the validators (they'd poison closed_trades + 500 later).
    # A real client CAN send the literal `Infinity` token in a raw body — json.loads parses it to inf.
    c, db = env
    _mk_manual_position(db)
    hdr = {"content-type": "application/json"}
    body = ('{"mint": "%s", "side": "sell", "kind": "take_profit", '
            '"trigger_type": "mult_at_or_above", "trigger_value": Infinity, '
            '"size_kind": "token_frac", "size_value": 0.5}') % MINT
    r = c.post("/api/manual/order", content=body, headers=hdr)
    assert r.status_code == 422
    body2 = ('{"mint": "%s", "side": "buy", "kind": "market", '
             '"size_kind": "usd", "size_value": NaN}') % MINT
    r2 = c.post("/api/manual/order", content=body2, headers=hdr)
    assert r2.status_code == 422


def test_non_string_note_and_ticker_tolerated(env):
    # review L4: a non-string note/ticker must coerce, not 500
    c, _ = env
    r = c.post("/api/manual/order", json={"mint": MINT, "side": "buy", "kind": "market",
                                          "size_kind": "usd", "size_value": 3.0,
                                          "note": 12345, "ticker": 99})
    assert r.status_code == 200


def test_manual_cap_zero_blocks_buy(env):
    # review L2: a cap of 0 disables manual buys (not "unlimited")
    c, db = env
    st = LiveState(db); st.set_system("manual_cap_usd", "0"); st.close()
    r = c.post("/api/manual/order", json={"mint": MINT, "side": "buy", "kind": "market",
                                          "size_kind": "usd", "size_value": 3.0})
    assert r.status_code == 409 and "disabled" in r.json()["error"].lower()


def test_takeover_and_release(env):
    c, db = env
    st = LiveState(db)
    st.create_position(mint=MINT, ticker="TOK", signal_at=NOW, signal_price=1.0, state="ENTERED")
    st.update_position(MINT, controller="algo", entry_price=1.0, remaining_frac=1.0)
    st.close()
    r = c.post(f"/api/positions/{MINT}/takeover")
    assert r.status_code == 200
    st = LiveState(db)
    assert st.get_position(MINT)["controller"] == "manual"
    assert int(st.get_system("controller_rev")) >= 1
    st.close()
    assert c.post(f"/api/positions/{MINT}/takeover").status_code == 409   # already manual
    r = c.post(f"/api/positions/{MINT}/release")
    assert r.status_code == 200
    st = LiveState(db); assert st.get_position(MINT)["controller"] == "algo"; st.close()


def test_inject_signal_queues_pending(env, monkeypatch):
    # ADD WATCHLIST: inject a token as a call -> queued pending -> engine picks it up
    c, db = env

    class FakePrice:
        def price_full(self, mints):
            return {MINT: {"usdPrice": 1.5}}

    monkeypatch.setattr(appmod, "_price_client", FakePrice())
    r = c.post("/api/signal", json={"mint": MINT, "ticker": "TOK"})
    assert r.status_code == 200 and r.json()["price"] == 1.5
    st = LiveState(db)
    pend = st.pending_manual_signals()
    st.close()
    assert len(pend) == 1 and pend[0]["mint"] == MINT and pend[0]["price"] == 1.5


def test_inject_signal_invalid_mint(env):
    c, _ = env
    assert c.post("/api/signal", json={"mint": "short"}).status_code == 422
