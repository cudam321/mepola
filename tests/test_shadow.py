"""ShadowRider/ShadowEngine — the challenger forward race must equal the golden sim, offline.

The whole point of the shadow layer is that challenger evidence is trustworthy BECAUSE it is
forward-only and pinned to the same sim semantics as the research. These tests pin:
  * C1 (champion) == a plain TailRider, exactly — the race's baseline is the live strategy;
  * C7 (dip=0 chase control) == sim with dip=0 (first-candle entry at open*1.01);
  * C2 (re-entry) == sim's leg list on a stop-then-recover path, one shadow_trades ROW PER LEG;
  * crash between candles -> rehydrate -> resume -> identical final trades;
  * a poisoned rider can never break the tick path (log + one alert, rider dropped).

v2 additions pin the widened race:
  * TrailExit == analysis/exit_sim.simulate_exit on synthetic series (exit_sim IS the
    oracle for trailing/time-stop exits — tp-then-trail, hard stop, time stop);
  * delay_1h enters at the FIRST candle at/after t0+1h, at THAT candle's open*1.01;
  * C11/C12/C18 == sim with their params; the C17 heat gate abstains under 5 resolved
    records and gates on seeded heat; TrailExit riders survive a mid-flight crash.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from memebot.analysis.exit_sim import ExitPolicy, simulate_exit
from memebot.live.shadow import CHALLENGERS, ShadowEngine, ShadowRider, TrailExit
from memebot.live.state import LiveState
from memebot.live.strategy import TailRider
from memebot.models import Candle, PriceSeries

from sim_oracle import sim

T0 = datetime(2026, 6, 1, 0, 0, 0, tzinfo=timezone.utc)
CFG = {c.id: c for c in CHALLENGERS}


def _c(i, o, h, l, cl):
    return Candle(ts=T0 + timedelta(minutes=i), open=o, high=h, low=l, close=cl, volume=1.0)


def _arrays(candles):
    H = np.array([c.high for c in candles]); L = np.array([c.low for c in candles])
    C = np.array([c.close for c in candles]); T = np.array([c.ts.timestamp() for c in candles])
    return H, L, C, T


def _moon():
    # sig=100 -> dip to 50 (enter 50.5) -> 3x secure -> 6x ride -> settle
    return [
        _c(0, 100, 100, 100, 100),
        _c(1, 100, 100, 50, 60),
        _c(2, 60, 4 * 50.5, 55, 3.5 * 50.5),
        _c(3, 3.5 * 50.5, 7 * 50.5, 3.0 * 50.5, 6.5 * 50.5),
        _c(4, 6.5 * 50.5, 6.5 * 50.5, 5 * 50.5, 5.5 * 50.5),
    ]


def _stop_then_recover():
    # enter 50.5 -> stop 35.35 (leg1 = 0.665) -> recover through 3*35.35=106.05 -> leg2 secures
    return [
        _c(0, 100, 100, 100, 100),
        _c(1, 100, 100, 50, 60),
        _c(2, 60, 60, 30, 32),
        _c(3, 32, 40, 30, 35),
        _c(4, 35, 110, 90, 108),
        _c(5, 108, 350, 100, 340),
    ]


def test_c1_champion_parity_with_plain_tailrider_and_sim():
    candles = _moon()
    r = ShadowRider(CFG["C1"], sig=100.0, t0=candles[0].ts.timestamp())
    tr = TailRider(sig=100.0, t0=candles[0].ts.timestamp())
    for c in candles:
        r.on_candle(c)
        tr.on_candle(c)
    r.finalize(candles[-1].close, candles[-1].ts)
    tr.finalize(candles[-1].close, candles[-1].ts)
    assert r.done and len(r.legs) == 1
    assert r.legs[0]["multiple"] == tr.realized_multiple      # EXACT: same machine
    H, L, C, T = _arrays(candles)
    oracle = sim(H, L, C, T, 100.0, 0.5, 0.7, 3.0, 0.33, None)
    assert r.legs[0]["multiple"] == pytest.approx(oracle[0], rel=1e-12)


def test_c7_dip0_enters_first_candle_and_matches_sim():
    candles = [
        _c(0, 100, 105, 95, 100),
        _c(1, 100, 120, 90, 110),
        _c(2, 110, 330, 100, 320),     # 3x=303 (entry 101) -> secure
        _c(3, 320, 650, 300, 600),     # 6x=606 -> ride
    ]
    r = ShadowRider(CFG["C7"], sig=100.0, t0=candles[0].ts.timestamp())
    kinds = r.on_candle(candles[0])
    assert "ENTER" in kinds
    assert r.cur.entry == pytest.approx(100 * 1.01)           # first-candle open*1.01, no dip wait
    for c in candles[1:]:
        r.on_candle(c)
    r.finalize(candles[-1].close, candles[-1].ts)
    H, L, C, T = _arrays(candles)
    oracle = sim(H, L, C, T, 100.0, 0, 0.7, 3.0, 0.33, None)
    assert len(r.legs) == 1
    assert r.legs[0]["multiple"] == pytest.approx(oracle[0], rel=1e-12)


def test_c2_reentry_matches_sim_legs_and_writes_one_row_per_leg(tmp_path):
    candles = _stop_then_recover()
    H, L, C, T = _arrays(candles)
    expected = sim(H, L, C, T, 100.0, 0.5, 0.7, 3.0, 0.33, 3.0)
    assert len(expected) == 2                                  # the path really has two legs
    assert expected[0] == pytest.approx(0.7 * 0.95)            # leg 1: stopped

    r = ShadowRider(CFG["C2"], sig=100.0, t0=candles[0].ts.timestamp())
    for c in candles:
        r.on_candle(c)
    r.finalize(candles[-1].close, candles[-1].ts)
    got = [leg["multiple"] for leg in r.legs]
    assert len(got) == len(expected)
    for g, e in zip(got, expected):
        assert g == pytest.approx(e, rel=1e-12)                # sequence, leg by leg
    assert sum(got) == pytest.approx(sum(expected), rel=1e-12)

    # engine-level: one shadow_trades ROW PER LEG (each an independent 1.0-notional entry)
    st = LiveState(tmp_path / "s.db")
    sh = ShadowEngine(st, configs=[CFG["C2"]])
    sh.ingest("MINTreentry", 100.0, candles[0].ts.timestamp(), ticker="AAA")
    for c in candles:
        sh.on_candle("MINTreentry", c)
    sh.finalize("MINTreentry", candles[-1].close, candles[-1].ts)
    rows = st.shadow_trades_by_config()["C2"]
    assert len(rows) == 2
    assert rows[0]["close_reason"] == "stopped"
    assert rows[1]["close_reason"] == "rode_to_horizon"
    assert [row["realized_multiple"] for row in rows] == pytest.approx(expected, rel=1e-12)
    assert st.load_shadow_riders() == []                       # terminal rider freed
    st.close()


def test_c2_stop_leg_flushes_immediately_not_at_retire(tmp_path):
    """REGRESSION (2026-07-04): a re-entry rider used to hoard stopped legs in its
    snapshot until FINAL retire — C2 showed realized $0 in the lab while champion C1
    showed the same 5 stop losses. A settled leg must land in shadow_trades NOW."""
    candles = _stop_then_recover()
    st = LiveState(tmp_path / "s.db")
    sh = ShadowEngine(st, configs=[CFG["C2"]])
    sh.ingest("MINTflush", 100.0, candles[0].ts.timestamp(), ticker="AAA")
    for c in candles[:3]:                          # through the stop candle only
        sh.on_candle("MINTflush", c)
    rows = st.shadow_trades_by_config().get("C2", [])
    assert len(rows) == 1                          # the loss is visible immediately
    assert rows[0]["close_reason"] == "stopped"
    riders = st.load_shadow_riders()
    assert len(riders) == 1                        # ...while the rider still races
    assert riders[0]["state"] == "AWAIT_REENTRY"
    assert json.loads(riders[0]["snapshot_json"])["flushed"] == 1

    for c in candles[3:]:                          # recover -> re-enter -> ride
        sh.on_candle("MINTflush", c)
    sh.finalize("MINTflush", candles[-1].close, candles[-1].ts)
    rows = st.shadow_trades_by_config()["C2"]
    assert len(rows) == 2                          # leg 1 NOT duplicated at retire
    assert [r["close_reason"] for r in rows] == ["stopped", "rode_to_horizon"]
    st.close()


def test_rehydrate_flushes_legacy_buffered_legs_once(tmp_path):
    """Pre-flush snapshots (no 'flushed' key) carry buffered legs; the first rehydrate
    must flush them exactly once — a second rehydrate must not duplicate."""
    candles = _stop_then_recover()
    r = ShadowRider(CFG["C2"], sig=100.0, t0=candles[0].ts.timestamp(), ticker="AAA")
    for c in candles[:3]:
        r.on_candle(c)
    assert len(r.legs) == 1 and r.awaiting_target is not None
    snap = r.snapshot()
    del snap["flushed"]                            # simulate a pre-fix production snapshot

    st = LiveState(tmp_path / "s.db")
    st.upsert_shadow_rider("C2", "MINTlegacy", snap, "AWAIT_REENTRY")
    ShadowEngine(st, configs=[CFG["C2"]])          # rehydrate #1 -> flush
    assert len(st.shadow_trades_by_config()["C2"]) == 1
    ShadowEngine(st, configs=[CFG["C2"]])          # rehydrate #2 -> no duplicate
    assert len(st.shadow_trades_by_config()["C2"]) == 1
    st.close()


def test_engine_persists_and_rehydrates_midflight(tmp_path):
    candles = _moon()
    # reference: straight-through run, no crash
    st_a = LiveState(tmp_path / "a.db")
    sh_a = ShadowEngine(st_a)
    sh_a.ingest("MINTpump", 100.0, candles[0].ts.timestamp(), ticker="AAA")
    for c in candles:
        sh_a.on_candle("MINTpump", c)
    sh_a.finalize("MINTpump", candles[-1].close, candles[-1].ts)
    ref = {cid: [r["realized_multiple"] for r in rows]
           for cid, rows in st_a.shadow_trades_by_config().items()}
    # every challenger that CAN trade this path traded it: C12's -60% dip never arrives,
    # C13's 1h-delay mark is past the 5-minute path, C17 is heat-gated out (abstains).
    assert set(ref) == {c.id for c in CHALLENGERS} - {"C12", "C13", "C17"}
    st_a.close()

    # crash between candle 2 and 3, rebuild from SQLite, resume, finalize
    st = LiveState(tmp_path / "b.db")
    sh = ShadowEngine(st)
    sh.ingest("MINTpump", 100.0, candles[0].ts.timestamp(), ticker="AAA")
    for c in candles[:3]:
        sh.on_candle("MINTpump", c)
    st.close()

    st2 = LiveState(tmp_path / "b.db")
    sh2 = ShadowEngine(st2)
    assert "MINTpump" in sh2.riders and "C1" in sh2.riders["MINTpump"]   # rehydrated
    for c in candles[3:]:
        sh2.on_candle("MINTpump", c)
    sh2.finalize("MINTpump", candles[-1].close, candles[-1].ts)
    got = {cid: [r["realized_multiple"] for r in rows]
           for cid, rows in st2.shadow_trades_by_config().items()}
    assert set(got) == set(ref)
    for cid in ref:
        assert got[cid] == pytest.approx(ref[cid], rel=1e-12)  # same final result
    assert st2.load_shadow_riders() == []
    st2.close()


def test_rider_exception_does_not_propagate(tmp_path):
    st = LiveState(tmp_path / "s.db")
    sh = ShadowEngine(st)
    sh.ingest("MINTpump", 100.0, T0.timestamp(), ticker="AAA")

    def boom(candle):
        raise RuntimeError("poisoned candle")

    sh.riders["MINTpump"]["C3"].on_candle = boom
    sh.on_candle("MINTpump", _c(1, 100, 100, 50, 60))          # must NOT raise
    assert "C3" not in sh.riders["MINTpump"]                   # poisoned rider dropped
    assert sh.riders["MINTpump"]["C1"].cur.entry == pytest.approx(50.5)  # others advanced
    n_alerts = len([a for a in st.recent_alerts() if a["kind"] == "SHADOW_ERROR"])
    assert n_alerts == 1                                       # alerted exactly once
    sh.on_candle("MINTpump", _c(2, 60, 61, 59, 60))            # continues normally
    assert len([a for a in st.recent_alerts() if a["kind"] == "SHADOW_ERROR"]) == 1
    st.close()


# --------------------------------------------------------------------------- #
# v2: TrailExit parity — analysis/exit_sim.simulate_exit is the oracle
# --------------------------------------------------------------------------- #

def _series(candles):
    return PriceSeries(mint="M", pool=None, timeframe="minute", aggregate=1, candles=candles)


def _trail_parity(policy: dict, candles, entry: float, t_fill):
    """Feed TrailExit incrementally; run simulate_exit offline; return both multiples."""
    tx = TrailExit(policy, entry=entry, t_fill=t_fill.timestamp())
    for c in candles:
        if c.ts >= t_fill:
            tx.on_candle(c)
    if not tx.done:
        tx.finalize(candles[-1].close)
    pol = ExitPolicy("parity", [tuple(r) for r in policy["tp_ladder"]], policy["stop_mult"],
                     policy["trail_pct"], policy["trail_arm_mult"], policy["time_stop_h"])
    return tx.realized_multiple, simulate_exit(_series(candles), entry, t_fill, pol)


def test_trailexit_parity_tp_then_trail():
    # P_MOON shape: sell 50% at 2x, then a 60% give-back trail (armed at 2x) takes the rest
    pol = {"tp_ladder": [(2.0, 0.5)], "stop_mult": 0.0, "trail_pct": 0.60,
           "trail_arm_mult": 2.0, "time_stop_h": 336.0}
    cds = [
        _c(0, 10, 11, 9.5, 10.5),
        _c(1, 10.5, 25, 10, 24),      # 2x rung fills; peak 25 arms the trail
        _c(2, 24, 30, 22, 28),        # rides; peak 30
        _c(3, 28, 29, 11, 12),        # low 11 <= 0.4*30 = 12 -> trailing give-back exit
    ]
    got, oracle = _trail_parity(pol, cds, 10.0, cds[0].ts)
    assert got == pytest.approx(oracle, rel=1e-12)
    expected = (0.5 * 20 * 0.985 + 0.5 * 12 * 0.96) / 10.0
    assert got == pytest.approx(expected, rel=1e-12)


def test_trailexit_parity_hard_stop():
    # the hard stop (bar low, BEFORE any TP — pessimistic) takes the whole position
    pol = {"tp_ladder": [(3.0, 0.33)], "stop_mult": 0.7, "trail_pct": 0.50,
           "trail_arm_mult": 3.0, "time_stop_h": 1e9}
    cds = [
        _c(0, 10, 11, 8, 9),
        _c(1, 9, 12, 6.9, 7.5),       # low 6.9 <= 0.7*10 = 7 -> stop at 7*(1-0.04)
    ]
    got, oracle = _trail_parity(pol, cds, 10.0, cds[0].ts)
    assert got == pytest.approx(oracle, rel=1e-12)
    assert got == pytest.approx(0.7 * 0.96, rel=1e-12)


def test_trailexit_parity_time_stop():
    # no new high for MORE than time_stop_h (strict >) -> remainder exits at the bar close
    pol = {"tp_ladder": [], "stop_mult": 0.0, "trail_pct": 1.0,
           "trail_arm_mult": 1e9, "time_stop_h": 1.0}
    cds = [
        _c(0, 10, 12, 9, 10),         # peak 12 -> the no-new-high clock starts here
        _c(30, 10, 11, 9, 10),        # +30m: nothing
        _c(60, 10, 11.5, 9, 10),      # +60m: exactly 1h since the high -> strict >, NOT yet
        _c(90, 10, 11, 9, 9.5),       # +90m: time stop fires at close*(1-tp_cost)
    ]
    got, oracle = _trail_parity(pol, cds, 10.0, cds[0].ts)
    assert got == pytest.approx(oracle, rel=1e-12)
    assert got == pytest.approx(0.95 * 0.985, rel=1e-12)


@pytest.mark.parametrize("cid", ["C14", "C15", "C16"])
def test_exit_family_end_to_end_matches_exit_sim(cid):
    """The exit-family configs (TrailExit exits) fed through the FULL ShadowRider path —
    dip entry then TrailExit hand-off — must equal analysis/exit_sim.simulate_exit run
    from the same fill. Pins each config's ACTUAL policy dict, not a hand-made one."""
    cc = CFG[cid]
    # a path that dips (enter 50.5), rungs up past 3x, then gives back — exercises the
    # tp ladder, the trail arm, and a give-back exit for all three exit configs
    cds = [
        _c(0, 100, 100, 100, 100),
        _c(1, 100, 100, 50, 60),          # dip to 50 -> enter 50.5 at this bar
        _c(2, 60, 5 * 50.5, 55, 4 * 50.5),   # rungs through 2x/3x; peak 252.5
        _c(3, 4 * 50.5, 4 * 50.5, 1.4 * 50.5, 1.6 * 50.5),  # deep give-back -> trail/stop bites
        _c(4, 1.6 * 50.5, 1.7 * 50.5, 1.3 * 50.5, 1.5 * 50.5),
    ]
    r = ShadowRider(cc, sig=100.0, t0=cds[0].ts.timestamp())
    for c in cds:
        r.on_candle(c)
    if not r.done:
        r.finalize(cds[-1].close, cds[-1].ts)
    got = [leg["multiple"] for leg in r.legs]
    assert len(got) == 1, f"{cid}: expected one leg, got {got}"

    ep = cc.exit_policy
    pol = ExitPolicy(cid, [tuple(x) for x in ep["tp_ladder"]], ep["stop_mult"],
                     ep["trail_pct"], ep["trail_arm_mult"], ep["time_stop_h"])
    oracle = simulate_exit(_series(cds), 50.5, cds[1].ts, pol)
    assert got[0] == pytest.approx(oracle, rel=1e-12), f"{cid}: {got[0]} vs exit_sim {oracle}"


# --------------------------------------------------------------------------- #
# v2: entry family — delay_1h and the new dip depths
# --------------------------------------------------------------------------- #

def test_c13_delay_1h_enters_right_candle_and_price_then_number1_exits():
    cds = [
        _c(0, 100, 120, 90, 110),
        _c(30, 110, 130, 100, 120),    # +30m: before the mark -> no entry
        _c(60, 80, 250, 75, 240),      # +1h EXACTLY: first candle at/after t0+3600 -> enter 80*1.01
        _c(90, 240, 300, 200, 280),
    ]
    r = ShadowRider(CFG["C13"], sig=100.0, t0=cds[0].ts.timestamp())
    assert r.on_candle(cds[0]) == []
    assert r.on_candle(cds[1]) == []
    assert r.status == "PENDING_ENTRY"
    kinds = r.on_candle(cds[2])
    assert "ENTER" in kinds
    assert r.cur.entry == pytest.approx(80 * 1.01)     # THAT candle's open, +1% — not sig, not close
    r.on_candle(cds[3])
    r.finalize(cds[-1].close, cds[-1].ts)
    # parity: the delayed leg == sim's dip==0 chase re-based at the 1h-mark candle
    H, L, C, T = _arrays(cds[2:])
    oracle = sim(H, L, C, T, 80.0, 0, 0.7, 3.0, 0.33, None)
    assert len(r.legs) == 1
    assert r.legs[0]["multiple"] == pytest.approx(oracle[0], rel=1e-12)


def _staircase_dip():
    # a stepped dip so -40% / -50% / -60% entries all trigger on DIFFERENT bars
    return [
        _c(0, 100, 100, 100, 100),
        _c(1, 100, 100, 55, 58),       # -45%: C11 (dip .4) enters at 60.6
        _c(2, 58, 60, 48, 50),         # -52%: dip .5 configs enter at 50.5
        _c(3, 50, 52, 39, 42),         # -61%: C12 enters at 40.4; C11's -30% stop (42.42) hits
        _c(4, 42, 320, 40, 300),       # the pump: rungs fill
        _c(5, 300, 310, 250, 260),
    ]


@pytest.mark.parametrize("cid,params", [
    ("C11", (0.4, 0.7, 3.0, 0.33)),
    ("C12", (0.6, 0.7, 3.0, 0.33)),
    ("C18", (0.5, 0.7, 1.5, 0.5)),
])
def test_new_sim_space_challengers_match_oracle(cid, params):
    cds = _staircase_dip()
    H, L, C, T = _arrays(cds)
    dip, sl, ftp, fsell = params
    expected = sim(H, L, C, T, 100.0, dip, sl, ftp, fsell, None)
    r = ShadowRider(CFG[cid], sig=100.0, t0=cds[0].ts.timestamp())
    for c in cds:
        r.on_candle(c)
    r.finalize(cds[-1].close, cds[-1].ts)
    assert len(r.legs) == len(expected) == 1
    assert r.legs[0]["multiple"] == pytest.approx(expected[0], rel=1e-12)


# --------------------------------------------------------------------------- #
# v2: gate family — the forward-safe heat gate (C17)
# --------------------------------------------------------------------------- #

def _heat_records(n, peak, now_epoch):
    # resolved records: ingested 2 days before `now` (>24h old, inside the 14d window)
    return {f"m{i}": {"t0": now_epoch - 2 * 86400, "sig": 1.0, "peak": peak} for i in range(n)}


def test_c17_gate_abstains_under_five_resolved_records(tmp_path):
    st = LiveState(tmp_path / "s.db")
    st.set_system("shadow_heat", json.dumps(_heat_records(4, 3.0, T0.timestamp())))  # hot but only 4
    sh = ShadowEngine(st)
    assert sh.market_heat(T0.timestamp()) is None                  # < 5 resolved -> unknown
    sh.ingest("MINTa", 100.0, T0.timestamp(), ticker="AAA")
    assert "C17" not in sh.riders["MINTa"]                         # gate ABSTAINS: no rider
    assert "C1" in sh.riders["MINTa"]                              # everyone else races
    assert all(row["config_id"] != "C17" for row in st.load_shadow_riders())
    assert [a for a in st.recent_alerts() if a["kind"] == "SHADOW_ERROR"] == []  # not noisy
    st.close()


def test_c17_gate_opens_when_hot_and_stays_shut_when_cold(tmp_path):
    hot = LiveState(tmp_path / "hot.db")
    hot.set_system("shadow_heat", json.dumps(_heat_records(6, 2.0, T0.timestamp())))
    sh = ShadowEngine(hot)
    assert sh.market_heat(T0.timestamp()) == pytest.approx(2.0)
    sh.ingest("MINTa", 100.0, T0.timestamp(), ticker="AAA")
    assert "C17" in sh.riders["MINTa"]                             # 2.0 >= 1.5 -> active
    assert sh.riders["MINTa"]["C17"].cur is not None               # a plain #1 TailRider
    assert any(row["config_id"] == "C17" for row in hot.load_shadow_riders())
    hot.close()

    cold = LiveState(tmp_path / "cold.db")
    cold.set_system("shadow_heat", json.dumps(_heat_records(6, 1.2, T0.timestamp())))
    sh2 = ShadowEngine(cold)
    sh2.ingest("MINTa", 100.0, T0.timestamp(), ticker="AAA")
    assert "C17" not in sh2.riders["MINTa"]                        # 1.2 < 1.5 -> gated out
    cold.close()


def test_heat_records_track_first_24h_peak_only(tmp_path):
    st = LiveState(tmp_path / "s.db")
    sh = ShadowEngine(st)
    sh.ingest("MINTa", 100.0, T0.timestamp(), ticker="AAA")
    sh.on_candle("MINTa", _c(1, 100, 250, 90, 200))                # peak-vs-sig 2.5, inside 24h
    heat = json.loads(st.get_system("shadow_heat"))
    assert heat["MINTa"]["peak"] == pytest.approx(2.5)
    late = Candle(ts=T0 + timedelta(hours=25), open=1, high=10000, low=1, close=1, volume=1.0)
    sh.on_candle("MINTa", late)                                    # after resolve -> frozen
    heat = json.loads(st.get_system("shadow_heat"))
    assert heat["MINTa"]["peak"] == pytest.approx(2.5)
    st.close()


# --------------------------------------------------------------------------- #
# v2: TrailExit riders through the engine — crash-safety + oracle parity
# --------------------------------------------------------------------------- #

def _trail_path():
    return [
        _c(0, 100, 100, 100, 100),
        _c(1, 100, 100, 50, 60),       # -50% dip -> C14/C15 enter at 50.5
        _c(2, 60, 202, 55, 190),       # C14 2x TP fills + arms; C15 3x TP fills; peak 202
        _c(3, 190, 400, 180, 350),     # event-less bar EXCEPT the peak (MARK must persist it)
        _c(4, 350, 360, 100, 120),     # C14 trail 0.4*400=160 >= low; C15 trail 200 >= low
    ]


def test_trailexit_rider_snapshot_roundtrip_midflight(tmp_path):
    cds = _trail_path()
    cfgs = [CFG["C14"], CFG["C15"]]
    # reference: straight through, no crash
    st_a = LiveState(tmp_path / "a.db")
    sh_a = ShadowEngine(st_a, configs=cfgs)
    sh_a.ingest("M", 100.0, cds[0].ts.timestamp(), ticker="AAA")
    for c in cds:
        sh_a.on_candle("M", c)
    sh_a.finalize("M", cds[-1].close, cds[-1].ts)
    ref = {cid: [r["realized_multiple"] for r in rows]
           for cid, rows in st_a.shadow_trades_by_config().items()}
    assert set(ref) == {"C14", "C15"}
    st_a.close()

    # crash after candle 3 (mid-flight: entered, rung filled, peak raised), rebuild, resume
    st = LiveState(tmp_path / "b.db")
    sh = ShadowEngine(st, configs=cfgs)
    sh.ingest("M", 100.0, cds[0].ts.timestamp(), ticker="AAA")
    for c in cds[:4]:
        sh.on_candle("M", c)
    st.close()

    st2 = LiveState(tmp_path / "b.db")
    sh2 = ShadowEngine(st2, configs=cfgs)
    r14 = sh2.riders["M"]["C14"]
    assert r14.trail is not None and r14.trail.filled == [True]    # rung state survived
    assert r14.trail.peak == pytest.approx(400.0)                  # decision-critical watermark
    for c in cds[4:]:
        sh2.on_candle("M", c)
    sh2.finalize("M", cds[-1].close, cds[-1].ts)
    got = {cid: [r["realized_multiple"] for r in rows]
           for cid, rows in st2.shadow_trades_by_config().items()}
    assert set(got) == set(ref)
    for cid in ref:
        assert got[cid] == pytest.approx(ref[cid], rel=1e-12)      # crash changed nothing
    assert st2.load_shadow_riders() == []                          # terminal riders freed
    # ... and BOTH runs equal the offline simulate_exit oracle from the fill bar
    for cid, pol in (("C14", ExitPolicy("pmoon", [(2.0, 0.5)], 0.0, 0.60, 2.0, 336.0)),
                     ("C15", ExitPolicy("trail", [(3.0, 0.33)], 0.7, 0.50, 3.0, 1e9))):
        oracle = simulate_exit(_series(cds), 50.5, cds[1].ts, pol)
        assert ref[cid][0] == pytest.approx(oracle, rel=1e-12)
    st2.close()


def test_flush_replay_is_idempotent_via_unique_leg_index(tmp_path):
    """Crash window: legs commit before the flushed-watermark does. A replayed flush
    (stale watermark after a kill) must not duplicate rows — INSERT OR IGNORE + the
    unique leg index make it a no-op."""
    candles = _stop_then_recover()
    st = LiveState(tmp_path / "s.db")
    sh = ShadowEngine(st, configs=[CFG["C2"]])
    sh.ingest("MINTdup", 100.0, candles[0].ts.timestamp(), ticker="AAA")
    for c in candles[:3]:
        sh.on_candle("MINTdup", c)
    r = sh.riders["MINTdup"]["C2"]
    assert len(st.shadow_trades_by_config()["C2"]) == 1
    r.flushed = 0                                  # simulate the lost-watermark crash
    sh._flush_legs("C2", "MINTdup", r)             # replay
    assert len(st.shadow_trades_by_config()["C2"]) == 1
    st.close()


# -- the full sim-space sweep: EVERY 5-param challenger == the stage37 oracle -------- #
# (C13 delay-entry, C14-C16 TrailExit and C17 heat-gate live outside the sim's param
#  space and are pinned by their own dedicated tests above/below.)

SIM_SPACE = sorted(
    cid for cid, cc in CFG.items()
    if cc.exit_policy is None and cc.heat_min is None and cc.entry_mode != "delay_1h"
)


def _sweep_paths():
    return {
        # dip -> secure -> ride -> settle at horizon
        "moon": _moon(),
        # dip -> stop -> recover through the re-entry trigger -> second leg
        "stop_recover": _stop_then_recover(),
        # dip -> collapse toward zero (stops fire; no-stop configs ride it down)
        "bleed": [
            _c(0, 100, 100, 100, 100),
            _c(1, 100, 100, 55, 60),
            _c(2, 60, 62, 20, 25),
            _c(3, 25, 26, 5, 6),
            _c(4, 6, 7, 2, 3),
        ],
        # straight rally, never dips: dip configs EXPIRE (no trade), chase configs ride
        "rally_no_dip": [
            _c(0, 100, 110, 95, 105),
            _c(1, 105, 180, 100, 170),
            _c(2, 170, 400, 160, 390),
            _c(3, 390, 800, 350, 700),
        ],
    }


@pytest.mark.parametrize("pname", ["moon", "stop_recover", "bleed", "rally_no_dip"])
@pytest.mark.parametrize("cid", SIM_SPACE)
def test_every_sim_space_config_matches_oracle(cid, pname):
    candles = _sweep_paths()[pname]
    cc = CFG[cid]
    r = ShadowRider(cc, sig=100.0, t0=candles[0].ts.timestamp())
    for c in candles:
        r.on_candle(c)
    r.finalize(candles[-1].close, candles[-1].ts)
    H, L, C, T = _arrays(candles)
    # sim returns None for "no trade" (dip never arrived) — the rider's no-legs equivalent
    expected = sim(H, L, C, T, 100.0, cc.dip, cc.sl, cc.ftp, cc.fsell, cc.reentry) or []
    got = [leg["multiple"] for leg in r.legs]
    assert len(got) == len(expected), f"{cid}/{pname}: rider legs {got} vs sim {expected}"
    for g, e in zip(got, expected):
        assert g == pytest.approx(e, rel=1e-12), f"{cid}/{pname}"


def test_horizon_leg_replay_stable_across_crash(tmp_path, monkeypatch):
    """F22: a horizon leg's closed_at is a wall-clock finalize ts, so it MUST dedup on reboot.
    _retire persists the done-snapshot (carrying the buffered leg) BEFORE flush+delete, so if
    the rider row survives a crash (the delete never committed), rehydrate re-flushes the SAME
    closed_at and INSERT OR IGNORE dedups — instead of re-finalizing with a fresh ts twice."""
    st = LiveState(tmp_path / "s.db")
    eng = ShadowEngine(st, configs=(CFG["C1"],))
    eng.ingest("HORIZ", 100.0, T0.timestamp(), ticker="H")
    eng.on_candle("HORIZ", _c(1, 100, 100, 49, 60))            # dip -> C1 enters at 50.5
    # crash sim: the delete during _retire never commits, so the rider row survives finalize
    monkeypatch.setattr(st, "delete_shadow_rider", lambda *a, **k: None)
    eng.finalize("HORIZ", 60.0, ts=T0 + timedelta(hours=1))    # horizon leg, closed_at = T0+1h
    legs1 = st.query("SELECT closed_at FROM shadow_trades WHERE config_id='C1'")
    assert len(legs1) == 1                                     # one leg written
    # reboot on the SAME file: the surviving row rehydrates and the safety net re-flushes it
    st2 = LiveState(tmp_path / "s.db")
    ShadowEngine(st2, configs=(CFG["C1"],))
    legs2 = st2.query("SELECT closed_at FROM shadow_trades WHERE config_id='C1'")
    assert len(legs2) == 1                                     # DEDUPED, not doubled
    assert legs2[0]["closed_at"] == legs1[0]["closed_at"]      # same stored timestamp
    assert st2.query("SELECT 1 FROM shadow_riders WHERE mint='HORIZ'") == []  # retired on reboot
    st.close()
    st2.close()
