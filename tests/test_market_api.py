"""Market-data API tests — fully offline: the module-level KEYLESS Jupiter clients are
replaced with fakes (the dashboard must never touch the engine's keyed quota, and tests
must never touch the network at all)."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo root: `dashboard` is not installed

import dashboard.server.app as appmod  # noqa: E402
from memebot.live.state import LiveState  # noqa: E402
from memebot.models import Candle  # noqa: E402

MINT = "MintAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
NOW = datetime.now(timezone.utc)
SIGNAL_AT = NOW - timedelta(hours=2)


def _mk_candle(ts: datetime, px: float = 1.0) -> Candle:
    return Candle(ts=ts, open=px, high=px * 1.1, low=px * 0.9, close=px, volume=100.0)


class FakeCharts:
    """Deliberately IGNORES [start, end] — like the real datapi (see RESEARCH.md)."""

    def __init__(self, candles: list[Candle]) -> None:
        self.candles = candles
        self.calls = 0

    def fetch_candles(self, mint, interval, start, end, *, candles=1000):
        self.calls += 1
        return list(self.candles)


class FakePrice:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.calls = 0

    def price_full(self, mints):
        self.calls += 1
        return self.payload


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db = tmp_path / "live_state.db"
    st = LiveState(db)
    st.create_position(mint=MINT, ticker="TOK", signal_at=SIGNAL_AT, signal_price=2.0,
                       state="ENTERED")
    st.update_position(MINT, entry_price=1.0, stop_price=0.70)
    st.close()
    monkeypatch.setattr(appmod, "DB_PATH", str(db))
    # fresh caches per test — TTL entries must not leak across tests
    monkeypatch.setattr(appmod, "_candles_cache", {})
    monkeypatch.setattr(appmod, "_live_cache", {})
    in_window = [_mk_candle(NOW - timedelta(minutes=m)) for m in (60, 30, 5)]
    out_of_window = [_mk_candle(SIGNAL_AT - timedelta(days=20)),  # pre-signal history
                     _mk_candle(SIGNAL_AT - timedelta(days=5))]
    charts = FakeCharts(out_of_window + in_window)
    price = FakePrice({MINT: {"usdPrice": 1.23, "liquidity": 4567.0, "priceChange24h": -12.5}})
    monkeypatch.setattr(appmod, "_charts_client", charts)
    monkeypatch.setattr(appmod, "_price_client", price)
    with TestClient(appmod.app) as c:
        yield c, charts, price


def test_candles_clamped_to_window_and_shape(env):
    c, charts, _ = env
    r = c.get(f"/api/token/{MINT}/candles?range=call&interval=1m")
    assert r.status_code == 200
    j = r.json()
    assert j["mint"] == MINT and j["interval"] == "1m"
    lo = datetime.fromisoformat(j["from"])
    hi = datetime.fromisoformat(j["to"])
    assert lo == SIGNAL_AT - timedelta(minutes=90)   # pre-roll so the call context is visible
    # the 2 far-out-of-window candles the fake returned must be dropped (datapi lesson)
    assert len(j["candles"]) == 3
    for row in j["candles"]:
        ts = datetime.fromisoformat(row[0])
        assert lo <= ts <= hi
        assert len(row) == 6                          # [iso, open, high, low, close, volume]
    lv = j["levels"]
    assert lv["call"] == 2.0
    assert lv["entry_gate"] == 1.0                    # 0.5 * signal_price (the -50% dip gate)
    assert lv["entry"] == 1.0
    assert lv["stop"] == 0.70
    assert [rg["mult"] for rg in lv["rungs"]] == [3.0, 6.0, 12.0, 24.0, 48.0]
    assert lv["rungs"][0]["price"] == 3.0


def test_bad_range_and_interval_rejected(env):
    c, _, _ = env
    assert c.get(f"/api/token/{MINT}/candles?range=weird").status_code == 422
    assert c.get(f"/api/token/{MINT}/candles?range=call&interval=5m").status_code == 422


def test_unknown_mint_404(env):
    c, charts, _ = env
    r = c.get("/api/token/NoSuchMint1111111111111111111111/candles?range=call")
    assert r.status_code == 404
    assert charts.calls == 0                          # never hits the upstream for unknown mints


def test_candles_ttl_cache_single_upstream_call(env):
    c, charts, _ = env
    r1 = c.get(f"/api/token/{MINT}/candles?range=call&interval=1m")
    r2 = c.get(f"/api/token/{MINT}/candles?range=call&interval=1m")
    assert r1.status_code == r2.status_code == 200
    assert charts.calls == 1                          # second hit served from the TTL cache
    assert r1.json() == r2.json()


def test_max_range_capped_at_30d_span_and_auto_hour(env):
    c, _, _ = env
    st = LiveState(appmod.DB_PATH)
    st.create_position(mint="OldMint11111111111111111111111111", ticker="OLD",
                       signal_at=NOW - timedelta(days=40), signal_price=1.0, state="EXPIRED")
    st.close()
    j = c.get("/api/token/OldMint11111111111111111111111111/candles?range=max").json()
    lo = datetime.fromisoformat(j["from"])
    assert (datetime.now(timezone.utc) - lo) <= timedelta(days=30, minutes=1)  # 30d cap
    assert j["interval"] == "1h"                      # auto: 30d span <= 40d -> hourly


def test_live_route_and_ttl_cache(env):
    c, _, price = env
    j = c.get(f"/api/token/{MINT}/live").json()
    assert j["price"] == 1.23
    assert j["liquidity"] == 4567.0
    assert j["price_change_24h"] == -12.5
    assert "ts" in j
    c.get(f"/api/token/{MINT}/live")
    assert price.calls == 1                           # second hit served from the TTL cache


def test_live_nulls_when_jupiter_has_no_data(env, monkeypatch):
    c, _, _ = env
    monkeypatch.setattr(appmod, "_price_client", FakePrice({}))
    r = c.get("/api/token/BrandNewMint111111111111111111111/live")
    assert r.status_code == 200                       # 200 with nulls, never 500
    j = r.json()
    assert j["price"] is None
    assert j["liquidity"] is None
    assert j["price_change_24h"] is None
    assert "ts" in j
