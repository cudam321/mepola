"""Dashboard data-layer regression tests (no network).

The live engine's sampler writes bankroll rows WITHOUT the seed-only expectation columns;
stats() must stay None-safe (this 500'd the deployed dashboard on 2026-07-02).
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dashboard import data  # noqa: E402
from memebot.live.state import LiveState  # noqa: E402

T0 = datetime(2026, 6, 1, tzinfo=timezone.utc)


def _seeded_state(tmp_path):
    st = LiveState(tmp_path / "s.db")
    pid = st.create_position(mint="M1", ticker="A", signal_at=T0, signal_price=1.0, state="EXITED")
    st.record_close(position_id=pid, mint="M1", ticker="A", entry_at=T0, entry_price=1.0,
                    stake_usd=3.0, exit_at=T0, close_reason="stopped", realized_multiple=0.665,
                    pnl_usd=3.0 * (0.665 - 1), was_stopped=True)
    return st


def test_stats_survives_live_sampler_rows_without_expectation(tmp_path):
    st = _seeded_state(tmp_path)
    # a seed-style row WITH the fractional reference...
    st.sample_bankroll(ts=T0, realized_equity_usd=500.0, unrealized_equity_usd=500.0,
                       deployed_usd=0.0, dry_powder_usd=500.0, n_open=0, n_watching=0,
                       realized_pnl_cum_usd=0.0, expected_equity_usd=500.0,
                       expected_lo_usd=500.0, expected_hi_usd=500.0)
    # ...then a LIVE engine sampler row WITHOUT it (expected_* all None)
    st.sample_bankroll(ts=T0, realized_equity_usd=498.0, unrealized_equity_usd=498.0,
                       deployed_usd=3.0, dry_powder_usd=495.0, n_open=1, n_watching=2,
                       realized_pnl_cum_usd=-2.0)
    points = data.hero_points(st)
    s = data.stats(st, points)                    # must not raise
    # final_* are the BACKTEST SEED endpoints -> read from SEED rows only (the row with a
    # non-null expected_equity_usd); live engine heartbeats must not shadow them.
    assert s["final_fixed_usd"] == 500.0
    assert s["final_fractional_usd"] == 500.0
    st.close()


def test_full_snapshot_builds_on_live_only_db(tmp_path):
    """A DB with ONLY live-style rows (no seed columns at all) must snapshot cleanly."""
    st = _seeded_state(tmp_path)
    st.sample_bankroll(ts=T0, realized_equity_usd=499.0, unrealized_equity_usd=499.0,
                       deployed_usd=0.0, dry_powder_usd=499.0, n_open=0, n_watching=0,
                       realized_pnl_cum_usd=-1.0)
    snap = data.snapshot(st)
    # live-only DB has no SEED bankroll rows -> both fall back to the starting bankroll
    assert snap["stats"]["final_fixed_usd"] == 500.0
    assert snap["stats"]["final_fractional_usd"] == 500.0
    assert len(snap["hero"]) == 1
    st.close()


# --------------------------------------------------------------------------- #
# LIVE vs SEED discrimination (system_state.seeded_at) + the account block
# --------------------------------------------------------------------------- #
def _mixed_db(tmp_path):
    """seeded_at set; SEED rows stamped before it, LIVE rows after — hand-computed numbers.

    SEED : 1 closed trade  (mult 0.665, pnl -1.005)                 -> excluded from account
    LIVE : closed today    (mult 3.0,  pnl +6.0)
           closed 3d ago   (mult 0.0,  pnl -3.0)
           open RIDING     (stake 3.0, current_multiple 4.0 -> unrealized +9.0, deployed 3.0)
           WATCHING        (n_live_watching 1)
    """
    now = datetime.now(timezone.utc)
    seed_ts = now - timedelta(days=10)
    st = LiveState(tmp_path / "mixed.db")
    st.set_system("seeded_at", seed_ts.isoformat())

    pid = st.create_position(mint="S1", ticker="S1", signal_at=seed_ts - timedelta(days=2),
                             signal_price=1.0, state="EXITED")
    st.record_close(position_id=pid, mint="S1", ticker="S1", entry_at=seed_ts - timedelta(days=2),
                    entry_price=1.0, stake_usd=3.0, exit_at=seed_ts - timedelta(days=1),
                    close_reason="stopped", realized_multiple=0.665, pnl_usd=3.0 * (0.665 - 1),
                    was_stopped=True)

    pid = st.create_position(mint="L1", ticker="L1", signal_at=now - timedelta(hours=8),
                             signal_price=1.0, state="EXITED")
    st.record_close(position_id=pid, mint="L1", ticker="L1", entry_at=now - timedelta(hours=6),
                    entry_price=1.0, stake_usd=3.0, exit_at=now, close_reason="rode_to_horizon",
                    realized_multiple=3.0, pnl_usd=6.0)

    pid = st.create_position(mint="L2", ticker="L2", signal_at=now - timedelta(days=3, hours=2),
                             signal_price=1.0, state="STOPPED")
    st.record_close(position_id=pid, mint="L2", ticker="L2", entry_at=now - timedelta(days=3, hours=1),
                    entry_price=1.0, stake_usd=3.0, exit_at=now - timedelta(days=3),
                    close_reason="stopped", realized_multiple=0.0, pnl_usd=-3.0, was_stopped=True)

    st.create_position(mint="L3", ticker="L3", signal_at=now - timedelta(hours=3),
                       signal_price=1.0, state="WATCHING")   # opened_at=now -> live
    st.update_position("L3", state="RIDING", entry_price=0.5, stake_usd=3.0,
                       current_multiple=4.0, current_price=2.0, secured=1)

    st.create_position(mint="L4", ticker="L4", signal_at=now - timedelta(hours=1),
                       signal_price=1.0, state="WATCHING")
    return st, seed_ts


def test_account_math_on_mixed_seed_live_db(tmp_path):
    st, seed_ts = _mixed_db(tmp_path)
    a = data.account(st)
    assert a["start_usd"] == 500.0
    assert a["live_realized_pnl"] == 3.0          # +6.0 - 3.0 (seed -1.005 excluded)
    assert a["live_unrealized_pnl"] == 9.0        # 3.0 * (4.0 - 1)
    assert a["balance_usd"] == 512.0              # 500 + 3 + 9
    assert a["deployed_usd"] == 3.0
    assert a["dry_powder_usd"] == 500.0           # 500 + 3 - 3
    assert a["today_pnl_usd"] == 6.0              # only L1 closed today (UTC)
    assert a["today_pnl_basis"] == "realized_only"
    assert a["n_live_trades_closed"] == 2
    assert a["n_live_open"] == 1
    assert a["n_live_watching"] == 1
    assert a["live_since"] == seed_ts.isoformat()
    assert a["stake_usd"] == 3.0                  # no ctl override -> config default
    st.close()


def test_account_zero_live_balance_is_start(tmp_path):
    """With zero live activity the honest balance is exactly the starting bankroll."""
    st = _seeded_state(tmp_path)                  # one closed trade at T0 (2026-06-01)
    st.set_system("seeded_at", datetime.now(timezone.utc).isoformat())   # everything is seed
    a = data.account(st)
    assert a["balance_usd"] == 500.0
    assert a["live_realized_pnl"] == 0.0 and a["live_unrealized_pnl"] == 0.0
    assert a["deployed_usd"] == 0.0 and a["dry_powder_usd"] == 500.0
    assert a["n_live_trades_closed"] == 0 and a["n_live_open"] == 0
    st.close()


def test_stats_live_seed_split_counts(tmp_path):
    st, _ = _mixed_db(tmp_path)
    points = data.hero_points(st)
    assert sorted(p["source"] for p in points) == ["live", "live", "live", "seed"]
    s = data.stats(st, points)
    # F42/F44: scoped OUTCOME metrics are CLOSED-only (an open bag is not a resolved outcome).
    # live = 2 closed (L1=3.0, L2=0.0) + 1 open (L3, split out as n_open); seed = 1 closed.
    assert s["seed"]["n"] == 1 and s["live"]["n"] == 2 and s["all"]["n"] == 3
    assert s["live"]["n_open"] == 1               # the open RIDING position, not a scored trade
    assert s["live"]["best"] == 3.0               # closed-only best (the open 4.0x mark excluded)
    assert s["seed"]["mean"] == 0.665
    assert s["n_positions"] == 4                  # legacy top-level keys still there (all points)
    assert "win_rate" in s and "as_designed" in s
    st.close()


def test_daily_pnl_live_only(tmp_path):
    st, _ = _mixed_db(tmp_path)
    dp = data.daily_pnl(st)
    assert [d["n_closed"] for d in dp] == [1, 1]  # 3d-ago day, then today (sorted asc)
    assert dp[0]["realized_pnl"] == -3.0
    assert dp[-1]["realized_pnl"] == 6.0
    st.close()


def test_lab_missing_tables_returns_empty_shape(tmp_path):
    """The adaptive team's tables may not exist yet (deployed DBs predate the DDL) —
    lab() must return the empty-but-shaped dict, never raise."""
    st = LiveState(tmp_path / "bare.db")
    for tbl in ("shadow_trades", "shadow_riders", "research_runs"):
        st.conn.execute(f"DROP TABLE IF EXISTS {tbl}")     # simulate a pre-lab DB
    st.conn.commit()
    lb = data.lab(st)
    lb.pop("meta")                                         # static challenger labels, tested below
    assert lb == {"champion": "C1", "configs": {},
                  "last_research": None, "research_running": False}
    st.close()


def test_lab_meta_labels_from_challengers(tmp_path):
    """lab()["meta"] carries config_id -> {label, family} from shadow.CHALLENGERS (importable
    here), so the frontend stops hardcoding labels. 'family' defaults to "core" until the
    parallel shadow workstream adds the field."""
    st = LiveState(tmp_path / "labmeta.db")
    meta = data.lab(st)["meta"]
    assert "C1" in meta
    assert set(meta["C1"]) == {"label", "family"}
    assert meta["C1"]["label"]                             # non-empty human label
    assert all(set(v) == {"label", "family"} and v["family"] for v in meta.values())
    st.close()


def test_lab_reads_shadow_tables(tmp_path):
    st = LiveState(tmp_path / "lab.db")
    # the tables ship with state.py's DDL now, but create-if-absent keeps this test standalone
    st.conn.executescript("""
        CREATE TABLE IF NOT EXISTS shadow_trades(config_id TEXT, mint TEXT, ticker TEXT,
            entered_at TEXT, closed_at TEXT, realized_multiple REAL, close_reason TEXT);
        CREATE TABLE IF NOT EXISTS shadow_riders(config_id TEXT, mint TEXT, snapshot_json TEXT,
            state TEXT, updated_at TEXT);
        CREATE TABLE IF NOT EXISTS research_runs(ts TEXT, status TEXT, verdict_json TEXT);
    """)
    ins_t = ("INSERT INTO shadow_trades(config_id,mint,ticker,entered_at,closed_at,"
             "realized_multiple,close_reason) VALUES(?,?,?,?,?,?,?)")
    st.conn.execute(ins_t, ("C2", "m1", "A", "t", "t", 3.0, "tp"))
    st.conn.execute(ins_t, ("C2", "m2", "B", "t", "t", 0.5, "stop"))
    ins_r = ("INSERT INTO shadow_riders(config_id,mint,snapshot_json,state,updated_at) "
             "VALUES(?,?,?,?,?)")
    st.conn.execute(ins_r, ("C2", "m3", "{}", "RIDING", "t"))
    st.conn.execute(ins_r, ("C2", "m4", "{}", "EXITED", "t"))
    st.conn.execute("INSERT INTO research_runs(ts,status,verdict_json) VALUES(?,'running',NULL)",
                    (datetime.now(timezone.utc).isoformat(),))
    st.conn.commit()
    st.set_system("champion_config_id", "C2")
    lb = data.lab(st)
    assert lb["champion"] == "C2"
    c2 = lb["configs"]["C2"]
    assert c2["n_trades"] == 2 and c2["n_open"] == 1        # EXITED rider is terminal
    assert c2["mean"] == 1.75 and c2["drop_top1_mean"] == 0.5 and c2["win_rate"] == 0.5
    assert c2["sum_pnl_at_3usd"] == 4.5                     # 3.0*((3.0-1)+(0.5-1))
    assert lb["research_running"] is True
    assert lb["last_research"] is None                      # latest row has no verdict yet
    st.close()


def test_snapshot_includes_new_sections(tmp_path):
    st, _ = _mixed_db(tmp_path)
    snap = data.snapshot(st)
    for key in ("account", "daily_pnl", "signal_flow", "lab", "history", "equity"):
        assert key in snap
    assert snap["account"]["balance_usd"] == 512.0
    for scope in ("live", "seed", "all"):
        assert scope in snap["stats"]
    assert all("source" in p for p in snap["hero"])
    sf = snap["signal_flow"]
    assert set(sf) == {"calls_24h", "calls_7d", "entry_rate_pct", "watching_now",
                       "expired_no_dip_pct"}
    st.close()


def test_open_positions_enrichment(tmp_path):
    st, _ = _mixed_db(tmp_path)
    st.update_position("L4", current_price=0.75,
                       dip_deadline=(datetime.now(timezone.utc) + timedelta(hours=10)).isoformat())
    st.update_position("L3", next_rung_price=3.0, peak_price=2.5)
    rows = {r["mint"]: r for r in data.open_positions(st)}
    watch, ride = rows["L4"], rows["L3"]
    assert watch["dip_progress_pct"] == 50.0      # 1.0 -> 0.75 is half-way to the 0.50 trigger
    assert 9.9 <= watch["dip_deadline_h_left"] <= 10.0
    assert watch["age_h"] is not None and watch["source"] == "live"
    assert ride["dist_to_next_rung_pct"] == 50.0  # 2.0 -> 3.0
    assert ride["peak_multiple"] == 5.0           # 2.5 / 0.5 entry
    assert ride["dist_to_stop_pct"] is None       # secured -> no stop
    st.close()


def test_seed_trade_with_exit_after_stamp_stays_seed(tmp_path):
    """REGRESSION (2026-07-03): 21 seed trades whose exit CANDLES post-dated seeded_at by hours
    were misfiled as live (fake -$16 balance). Classification must follow SIGNAL time: a trade
    whose signal predates the seed stamp is SEED no matter when its exit candle lands."""
    now = datetime.now(timezone.utc)
    seed_ts = now - timedelta(days=1)
    st = LiveState(tmp_path / "leak.db")
    st.set_system("seeded_at", seed_ts.isoformat())
    pid = st.create_position(mint="SLEAK", ticker="SL", signal_at=seed_ts - timedelta(days=2),
                             signal_price=1.0, state="EXITED")
    # exit candle lands AFTER the seed stamp (the leak)
    st.record_close(position_id=pid, mint="SLEAK", ticker="SL", entry_at=seed_ts - timedelta(days=1, hours=12),
                    entry_price=1.0, stake_usd=3.0, exit_at=seed_ts + timedelta(hours=12),
                    close_reason="rode_to_horizon", realized_multiple=0.7, pnl_usd=-0.9)
    a = data.account(st)
    assert a["n_live_trades_closed"] == 0
    assert a["balance_usd"] == 500.0                      # the leak showed a fake loss here
    pts = data.hero_points(st)
    assert pts[0]["source"] == "seed"
    assert data.daily_pnl(st) == []
    st.close()


def test_bankroll_series_tags_seed_vs_live(tmp_path):
    """REGRESSION (2026-07-03): seed and live rows must stay separate series — stitching
    them produced a fake vertical jump at the seed/live seam on the equity chart."""
    st = LiveState(tmp_path / "curve.db")
    st.sample_bankroll(ts=T0, realized_equity_usd=898.0, unrealized_equity_usd=898.0,
                       deployed_usd=0.0, dry_powder_usd=898.0, n_open=0, n_watching=0,
                       realized_pnl_cum_usd=398.0, expected_equity_usd=741.0,
                       expected_lo_usd=741.0, expected_hi_usd=741.0)      # SEED row
    st.sample_bankroll(ts=T0 + timedelta(days=1), realized_equity_usd=500.0,
                       unrealized_equity_usd=497.5, deployed_usd=3.0, dry_powder_usd=497.0,
                       n_open=1, n_watching=0, realized_pnl_cum_usd=0.0)  # LIVE row
    rows = data.bankroll_series(st)
    assert rows[0]["fixed"] == 898.0 and rows[0]["live"] is None          # seed carries fixed/frac
    assert rows[1]["fixed"] is None and rows[1]["fractional"] is None
    assert rows[1]["live"] == 497.5                                       # live carries balance only
    st.close()


# --------------------------------------------------------------------------- #
# Trade history (closed trades + expired watchers), the equity split, /api/history
# --------------------------------------------------------------------------- #
def _history_db(tmp_path):
    """_mixed_db + one LIVE expired watcher (LX) and one SEED expired watcher (SX)."""
    st, seed_ts = _mixed_db(tmp_path)
    now = datetime.now(timezone.utc)
    st.create_position(mint="LX", ticker="LX", signal_at=now - timedelta(hours=50),
                       signal_price=1.0, state="WATCHING")
    st.update_position("LX", state="EXPIRED", close_reason="no_dip_within_48h",
                       closed_at=(now - timedelta(hours=2)).isoformat())
    st.create_position(mint="SX", ticker="SX", signal_at=seed_ts - timedelta(days=3),
                       signal_price=1.0, state="WATCHING")
    st.update_position("SX", state="EXPIRED", close_reason="no_dip_within_48h",
                       closed_at=(seed_ts - timedelta(days=2)).isoformat())
    return st, seed_ts


def test_trade_history_scopes_ordering_and_shapes(tmp_path):
    st, _ = _history_db(tmp_path)
    # newest-first across BOTH kinds: L1(now) > LX(-2h) > L2(-3d) > S1(-11d) > SX(-12d)
    rows = data.trade_history(st, "all", 100)
    assert [r["mint"] for r in rows] == ["L1", "LX", "L2", "S1", "SX"]
    assert [r["mint"] for r in data.trade_history(st, "live", 100)] == ["L1", "LX", "L2"]
    assert [r["mint"] for r in data.trade_history(st, "seed", 100)] == ["S1", "SX"]
    assert [r["mint"] for r in data.trade_history(st, "all", 2)] == ["L1", "LX"]  # limit

    trade = rows[0]                                        # L1: a realized live trade
    assert trade["kind"] == "trade" and trade["source"] == "live"
    assert trade["close_reason"] == "rode_to_horizon"
    assert trade["realized_multiple"] == 3.0 and trade["pnl_usd"] == 6.0
    assert trade["entry_price"] == 1.0
    assert trade["was_stopped"] is False and trade["was_secured"] is False

    exp = rows[1]                                          # LX: expired, never entered
    assert exp["kind"] == "expired" and exp["source"] == "live"
    assert exp["close_reason"] == "no dip within 48h"
    assert exp["realized_multiple"] is None and exp["pnl_usd"] == 0.0
    assert exp["entry_price"] is None
    st.close()


def test_snapshot_history_block(tmp_path):
    st, _ = _history_db(tmp_path)
    h = data.snapshot(st)["history"]
    assert set(h) == {"rows", "live_count", "seed_count"}
    assert h["live_count"] == 3 and h["seed_count"] == 2
    assert [r["mint"] for r in h["rows"]] == ["L1", "LX", "L2"]   # live slice only
    st.close()


def test_api_history_endpoint_validation_and_rows(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    import dashboard.server.app as appmod

    st, _ = _history_db(tmp_path)
    st.close()
    monkeypatch.setattr(appmod, "DB_PATH", str(tmp_path / "mixed.db"))
    with TestClient(appmod.app) as c:
        j = c.get("/api/history").json()                   # defaults: scope=live, limit=200
        assert j["scope"] == "live" and j["limit"] == 200
        assert [r["mint"] for r in j["rows"]] == ["L1", "LX", "L2"]
        j = c.get("/api/history?scope=seed&limit=1").json()
        assert [r["mint"] for r in j["rows"]] == ["S1"]
        assert len(c.get("/api/history?scope=all&limit=1000").json()["rows"]) == 5
        assert c.get("/api/history?scope=bogus").status_code == 422
        assert c.get("/api/history?limit=0").status_code == 422
        assert c.get("/api/history?limit=1001").status_code == 422
        assert c.get("/api/history?limit=notanint").status_code == 422   # FastAPI coercion


def test_equity_series_split(tmp_path):
    """LIVE and SEED equity are SEPARATE arrays (frontend tabs) — never one stitched curve."""
    st = LiveState(tmp_path / "eq.db")
    st.set_system("seeded_at", T0.isoformat())
    st.sample_bankroll(ts=T0, realized_equity_usd=898.0, unrealized_equity_usd=898.0,
                       deployed_usd=0.0, dry_powder_usd=898.0, n_open=0, n_watching=0,
                       realized_pnl_cum_usd=398.0, expected_equity_usd=741.0,
                       expected_lo_usd=741.0, expected_hi_usd=741.0)      # SEED row
    st.sample_bankroll(ts=T0 + timedelta(days=1), realized_equity_usd=500.0,
                       unrealized_equity_usd=497.5, deployed_usd=3.0, dry_powder_usd=497.0,
                       n_open=1, n_watching=0, realized_pnl_cum_usd=0.0)  # LIVE row
    rows = st.bankroll_series()
    eq = data.equity_series(st)
    assert set(eq) == {"live", "seed_fixed", "seed_frac", "live_since"}
    assert eq["seed_fixed"] == [[rows[0]["ts"], 898.0]]
    assert eq["seed_frac"] == [[rows[0]["ts"], 741.0]]
    assert eq["live"] == [[rows[1]["ts"], 497.5]]          # live rows only, [ts, balance] pairs
    assert eq["live_since"] == T0.isoformat()
    st.close()


def test_open_positions_plain_percent_fields(tmp_path):
    st, _ = _mixed_db(tmp_path)
    st.update_position("L4", current_price=0.75, low_price=0.6)
    rows = {r["mint"]: r for r in data.open_positions(st)}
    watch = rows["L4"]
    assert watch["pct_from_call"] == -25.0                 # 0.75 vs the 1.0 call (negative = down)
    assert watch["low_pct_from_call"] == -40.0             # deepest low 0.6 vs the call
    assert watch["dip_progress_pct"] == 50.0               # legacy fields kept for compatibility
    assert watch["dip_low_pct"] == 80.0
    assert rows["L3"]["pct_from_call"] is None             # non-WATCHING rows stay None
    st.close()


def test_lab_open_pnl_marks_open_riders(tmp_path):
    """REGRESSION (2026-07-03): the lab showed C1 'in loss' while the account balance was
    'in profit' — closed legs only, no mark-to-market of open riders. Each config's
    total_pnl must be realized + open so the champion row reconciles with the balance."""
    import json as _json
    st = LiveState(tmp_path / "lab2.db")
    # a closed C1 stop-out (-$1.005) ...
    st.conn.execute(
        "INSERT INTO shadow_trades(config_id,mint,ticker,entered_at,closed_at,"
        "realized_multiple,close_reason) VALUES('C1','DEAD','D','t','t',0.665,'stopped')")
    # ... and an open C1 rider on KEVIN riding at 3.464x (pr already banked + rem marked)
    snap = {"v": 2, "config_id": "C1", "sig": 2e-4, "t0": 0, "ticker": "KEVIN", "legs": [],
            "awaiting_target": None, "done": False, "trail": None, "cur_entered_at": None,
            "cur": {"state": "SECURED", "sig": 2e-4, "t0": 0, "entry": 1e-4, "stop_price": None,
                    "rem": 0.67, "pr": 0.0000985, "n_tp": 1, "lvl": 6.0, "secured": True,
                    "peak_price": 4e-4, "low_price": None}}
    st.conn.execute(
        "INSERT INTO shadow_riders(config_id,mint,snapshot_json,state,updated_at) "
        "VALUES('C1','KEVIN',?,'SECURED','t')", (_json.dumps(snap),))
    st.conn.commit()
    st.create_position(mint="KEVIN", ticker="KEVIN", signal_at=T0, signal_price=2e-4,
                       state="SECURED")
    st.update_position("KEVIN", current_price=3.7e-4)   # the latest tick price

    c1 = data.lab(st)["configs"]["C1"]
    assert c1["sum_pnl_at_3usd"] == -1.0                # closed leg (rounded)
    # open mark: (pr + rem*price)/entry = (0.0000985 + 0.67*3.7e-4)/1e-4 = 3.464x -> +$7.39
    assert abs(c1["open_pnl_at_3usd"] - 7.39) < 0.02
    assert abs(c1["total_pnl_at_3usd"] - (c1["sum_pnl_at_3usd"] + c1["open_pnl_at_3usd"])) < 0.001
    st.close()
