"""Control API tests — runtime knobs only; strategy params / mode must be rejected."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo root: `dashboard` is not installed

import dashboard.server.app as appmod  # noqa: E402
from memebot.live.state import LiveState  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db = tmp_path / "live_state.db"
    LiveState(db).close()  # seed: DDL + mode=paper, kill_switch=off
    monkeypatch.setattr(appmod, "DB_PATH", str(db))
    with TestClient(appmod.app) as c:
        yield c


def test_get_control_shape(client):
    r = client.get("/api/control")
    assert r.status_code == 200
    j = r.json()
    assert j["mode"] == "paper"
    assert "mode_note" in j
    ed = j["editable"]
    assert ed["kill_switch"]["value"] == "off"
    for key in ("ctl_stake_usd", "ctl_max_concurrent",
                "ctl_total_deployed_cap_usd", "ctl_daily_loss_cap_usd"):
        assert {"value", "min", "max", "default"} <= set(ed[key])
    assert ed["ctl_stake_usd"]["min"] == 0.5
    assert ed["ctl_stake_usd"]["max"] == 10.0          # STAKE_HARD_CAP_USD
    assert ed["ctl_stake_usd"]["value"] <= 10.0
    lk = j["locked"]
    assert lk["dip_trigger"] == 0.50
    assert lk["stop_level_mult"] == 0.70
    assert lk["reentry"] is False
    assert "note" in lk


def test_post_kill_switch_persists(client):
    r = client.post("/api/control", json={"key": "kill_switch", "value": "on"})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert client.get("/api/control").json()["editable"]["kill_switch"]["value"] == "on"


def test_post_stake_ok(client):
    r = client.post("/api/control", json={"key": "ctl_stake_usd", "value": 5})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "key": "ctl_stake_usd", "value": 5.0}
    assert client.get("/api/control").json()["editable"]["ctl_stake_usd"]["value"] == 5.0


def test_post_stake_above_hard_cap_rejected(client):
    r = client.post("/api/control", json={"key": "ctl_stake_usd", "value": 50})
    assert r.status_code == 422


def test_post_kill_switch_bad_value_rejected(client):
    r = client.post("/api/control", json={"key": "kill_switch", "value": "maybe"})
    assert r.status_code == 422


def test_post_mode_rejected(client):
    # `mode` is never settable from the UI (live arming is CLI-gated)
    r = client.post("/api/control", json={"key": "mode", "value": "live"})
    assert r.status_code == 400


def test_post_strategy_param_rejected(client):
    # strategy parameters are research-locked — no write path
    r = client.post("/api/control", json={"key": "dip_trigger", "value": 0.9})
    assert r.status_code == 400


def test_post_research_requested_ok(client):
    # one-shot flag the engine consumes+clears; only the string '1' is accepted
    r = client.post("/api/control", json={"key": "research_requested", "value": "1"})
    assert r.status_code == 200 and r.json()["ok"] is True


def test_post_research_requested_bad_value_rejected(client):
    r = client.post("/api/control", json={"key": "research_requested", "value": "2"})
    assert r.status_code == 422


def test_post_champion_config_id_is_disabled(client):
    # F38: champion promotion is NOT wired to the engine (it always trades C1), so accepting
    # the write would display a champion the engine isn't trading. It is rejected until real
    # engine support lands — even a "valid" C1..C10 value.
    for v in ("C3", "C1", "X99", "C11"):
        r = client.post("/api/control", json={"key": "champion_config_id", "value": v})
        assert r.status_code == 422
    # the seeded default stays C1
    assert client.get("/api/control").status_code == 200


# -- custom challengers (add_challenger / delete_challenger) ---------------------- #

def _add(client, **overrides):
    d = {"label": "my test strat", "dip": 0.4, "sl": 0.6, "ftp": 2.5,
         "fsell": 0.5, "reentry": None, "entry_mode": "dip"}
    d.update(overrides)
    return client.post("/api/control", json={"key": "add_challenger", "value": d})


def test_add_challenger_assigns_x_id_and_bumps_rev(client):
    r = _add(client)
    assert r.status_code == 200
    assert r.json()["id"] == "X1"
    r2 = _add(client, label="second")
    assert r2.json()["id"] == "X2"
    from memebot.live.shadow import CUSTOM_REV_KEY, load_custom_challengers
    st = LiveState(appmod.DB_PATH)
    try:
        customs = load_custom_challengers(st)
        assert [c.id for c in customs] == ["X1", "X2"]
        assert customs[0].family == "custom"
        assert st.get_system(CUSTOM_REV_KEY) == "2"
    finally:
        st.close()


@pytest.mark.parametrize("bad", [
    {"label": ""},                      # empty label
    {"dip": 0.99},                      # dip out of range
    {"sl": 1.5},                        # stop above entry
    {"ftp": 1.0},                       # TP not above entry
    {"fsell": 0.0},                     # sells nothing
    {"reentry": 0.5},                   # re-entry below the stop
    {"entry_mode": "yolo"},             # unknown mode
])
def test_add_challenger_rejects_bad_knobs(client, bad):
    assert _add(client, **bad).status_code == 422


def test_delete_challenger_removes_def_riders_and_trades(client):
    assert _add(client).json()["id"] == "X1"
    st = LiveState(appmod.DB_PATH)
    try:
        st.upsert_shadow_rider("X1", "MINTx", {"legs": []}, "WATCHING")
        st.record_shadow_trade(config_id="X1", mint="MINTx", realized_multiple=0.7,
                               close_reason="stopped")
    finally:
        st.close()
    r = client.post("/api/control", json={"key": "delete_challenger", "value": "X1"})
    assert r.status_code == 200 and r.json()["deleted"] is True
    st = LiveState(appmod.DB_PATH)
    try:
        assert st.load_shadow_riders() == []
        assert st.shadow_trades_by_config() == {}
        from memebot.live.shadow import load_custom_challengers
        assert load_custom_challengers(st) == ()
    finally:
        st.close()


def test_delete_challenger_rejects_builtins_and_unknown(client):
    for cid in ("C1", "C7", "X9"):
        r = client.post("/api/control", json={"key": "delete_challenger", "value": cid})
        assert r.status_code == 422


def test_lab_config_detail_endpoint(client):
    r = client.get("/api/lab/C1")
    assert r.status_code == 200
    j = r.json()
    assert j["params"]["label"] == "champion #1"
    assert j["params"]["dip"] == 0.5 and j["params"]["sl"] == 0.7
    assert client.get("/api/lab/NOPE").status_code == 404


def test_stream_endpoint_shape(client, monkeypatch):
    # FDV enrichment is background-warmed off the request path (F36), so SEED the supply cache
    # -> the request serves 1e9 tokens from cache synchronously (no network, no warm thread).
    import time as _t
    appmod._supply_cache["MINTs"] = (_t.monotonic() + 3600, 1e9)
    monkeypatch.setattr(appmod, "_supply_fetch", lambda mint: 1e9)   # neutralize any warm
    from datetime import datetime, timezone
    st = LiveState(appmod.DB_PATH)
    try:
        ts = datetime(2026, 7, 4, tzinfo=timezone.utc)
        pid = st.create_position(mint="MINTs", ticker="STRM", signal_at=ts,
                                 signal_price=100.0, state="WATCHING", t0_epoch=ts.timestamp(),
                                 dip_deadline=ts)
        st.update_position("MINTs", stake_usd=3.0)
        st.mark_seen("MINTs")                     # outcome != 'seen' -> live provenance
        st.set_system("seen_outcome_hack", "x")   # no-op, keeps set_system exercised
        st.conn.execute("UPDATE seen_mints SET outcome='entered' WHERE mint='MINTs'")
        st.conn.commit()
        st.append_event(position_id=pid, mint="MINTs", ts=ts, event_type="ENTER",
                        price=0.5, frac=0.0, remaining_frac=1.0)
        st.append_event(position_id=pid, mint="MINTs", ts=ts, event_type="TP", price=1.5,
                        rung_mult=3.0, frac=0.33, proceeds_usd=2.93, remaining_frac=0.67)
    finally:
        st.close()
    r = client.get("/api/stream?scope=live&limit=50")
    assert r.status_code == 200
    rows = r.json()["rows"]
    assert [x["action"] for x in rows] == ["TAKE PROFIT", "BUY"]     # newest first
    tp = rows[0]
    assert tp["value_usd"] == 2.93
    assert tp["pnl_usd"] == pytest.approx(2.93 - 0.33 * 3.0, abs=0.01)  # proceeds - cost basis
    assert tp["fdv_usd"] == pytest.approx(1.5e9)                     # price x supply
    buy = rows[1]
    assert buy["value_usd"] == 3.0 and buy["pnl_usd"] is None
    assert client.get("/api/stream?scope=bogus").status_code == 422
