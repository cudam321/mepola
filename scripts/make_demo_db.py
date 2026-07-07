#!/usr/bin/env python3
"""Build a POPULATED demo copy of the live DB for design review.

Clones runs/live_state.db and injects rich LIVE-provenance demo data so every dashboard
panel renders full: watchers at every dip stage, open positions (one ≥10x to light the
reserved incandescent), trade history, a busy 18-config shadow lab, alerts, live equity.
Never touches the real DB. Serve it with:

    MEMEBOT_DB=/tmp/demo_state.db PYTHONPATH=src:. uv run uvicorn dashboard.server.app:app --port 8010

    PYTHONPATH=src python3 scripts/make_demo_db.py [--out /tmp/demo_state.db]
"""
from __future__ import annotations

import argparse
import json
import random
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from memebot.live.state import LiveState  # noqa: E402

NOW = datetime.now(timezone.utc)
rng = random.Random(42)


def iso(dt):
    return dt.isoformat()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="/tmp/demo_state.db")
    args = ap.parse_args()

    dst_path = Path(args.out)
    # audit #33: this helper UNLINKS --out. Never let it clobber the real live DB (its own backup
    # source) or any *.db under runs/ — the docstring bills it as "never touches the real DB".
    runs_dir = (ROOT / "runs").resolve()
    if dst_path.resolve() == (runs_dir / "live_state.db") or (
            dst_path.suffix == ".db" and dst_path.resolve().parent == runs_dir):
        raise SystemExit(f"refusing to overwrite a real DB under runs/: {dst_path} — use a temp path")
    if dst_path.exists():
        dst_path.unlink()
    # Start from the schema LiveState itself creates (a fresh clone has no runs/ DB) and,
    # when a local live DB exists, clone it first so the demo carries its full shape.
    src_db = ROOT / "runs" / "live_state.db"
    if src_db.exists():
        src = sqlite3.connect(str(src_db))
        dst = sqlite3.connect(str(dst_path))
        src.backup(dst)
        src.close()
        dst.close()

    st = LiveState(dst_path)

    def seen(mint, ticker):
        st.mark_seen(mint, ticker=ticker, source_channel="@your_channel", message_id=1,
                     first_seen_at=NOW, outcome="positioned")   # LIVE provenance

    def signal(mint, ticker, ts):
        st.record_signal(ts=ts, source_channel="@your_channel", message_id=1, ticker=ticker,
                         mint=mint, side="buy", parse_confidence=1.0, is_first_call=True,
                         accepted=True, raw_text=f"buy ${ticker}")

    # ---- watchers at every dip stage -------------------------------------- #
    watchers = [
        ("DEMOW1", "WAGYU",  2.10e-4, -0.08, -0.15, 43.0),
        ("DEMOW2", "PONKE",  1.52e-4, -0.27, -0.33, 38.5),
        ("DEMOW3", "GIGA",   9.10e-5, -0.38, -0.44, 21.0),
        ("DEMOW4", "SHRIMP", 3.77e-4, -0.47, -0.485, 5.5),     # about-to-trigger glow
        ("DEMOW5", "MOONER", 6.40e-5, +0.52, -0.14, 46.0),     # ripping above call
    ]
    for mint, tick, sig, cur_pct, low_pct, hours_left in watchers:
        t_sig = NOW - timedelta(hours=48 - hours_left)
        seen(mint, tick); signal(mint, tick, t_sig)
        pid = st.create_position(mint=mint, ticker=tick, signal_at=t_sig, signal_price=sig,
                                 state="WATCHING", dip_deadline=t_sig + timedelta(hours=48),
                                 t0_epoch=t_sig.timestamp())
        st.append_event(position_id=pid, mint=mint, ts=t_sig, event_type="SIGNAL", price=sig,
                        note="first-call BUY -> WATCHING")
        st.update_position(mint, current_price=sig * (1 + cur_pct), low_price=sig * (1 + low_pct))

    # ---- open positions: ENTERED / SECURED / RIDING(>=10x!) --------------- #
    opens = [
        # mint, ticker, entry, cur_mult, state, secured, n_tp, lvl, rem, pr_units
        ("DEMOO1", "KEVIN", 9.24e-5, 0.92, "ENTERED", 0, 0, 3.0, 1.00, 0.0),
        ("DEMOO2", "BONKD", 1.10e-4, 3.46, "SECURED", 1, 1, 6.0, 0.67, 1.10e-4 * 3 * 0.33 * 0.985),
        ("DEMOO3", "ORCAT", 4.61e-5, 14.7, "RIDING",  1, 3, 24.0, 0.3769,
         4.61e-5 * (3 * 0.33 + 6 * 0.25 * 0.67 + 12 * 0.25 * 0.5025) * 0.985),
    ]
    for mint, tick, entry, mult, state_, sec, ntp, lvl, rem, pr in opens:
        t_sig = NOW - timedelta(hours=rng.uniform(6, 30))
        t_ent = t_sig + timedelta(hours=rng.uniform(1, 5))
        seen(mint, tick); signal(mint, tick, t_sig)
        pid = st.create_position(mint=mint, ticker=tick, signal_at=t_sig, signal_price=entry * 2,
                                 state="WATCHING", dip_deadline=t_sig + timedelta(hours=48),
                                 t0_epoch=t_sig.timestamp())
        st.append_event(position_id=pid, mint=mint, ts=t_sig, event_type="SIGNAL", price=entry * 2,
                        note="first-call BUY -> WATCHING")
        st.append_event(position_id=pid, mint=mint, ts=t_ent, event_type="ENTER", price=entry,
                        remaining_frac=1.0, note="-50% dip filled; hard stop armed")
        if ntp >= 1:
            st.append_event(position_id=pid, mint=mint, ts=t_ent + timedelta(hours=2),
                            event_type="TP", price=entry * 3, rung_mult=3.0, frac=0.33,
                            proceeds_usd=3.0 * 0.99 * 0.985, remaining_frac=0.67,
                            note="secured: sold 33% at 3x, stop removed")
        for k in range(2, ntp + 1):
            rung = 3 * (2 ** (k - 1))
            st.append_event(position_id=pid, mint=mint, ts=t_ent + timedelta(hours=2 + k),
                            event_type="RIDE_SELL", price=entry * rung, rung_mult=float(rung),
                            frac=0.25, proceeds_usd=1.5, remaining_frac=rem,
                            note=f"sold 25% of remainder at {rung}x")
        cur_price = entry * mult / (rem + pr / (entry * mult)) if rem else entry * mult
        st.update_position(mint, state=state_, entry_at=iso(t_ent), entry_price=entry,
                           stake_usd=3.0, tokens_qty=3.0 / entry,
                           stop_price=(0.7 * entry if not sec else None), secured=sec, n_tp=ntp,
                           next_rung_mult=lvl, next_rung_price=lvl * entry, remaining_frac=rem,
                           proceeds_units=pr, peak_price=entry * mult * 1.15, low_price=entry * 0.86,
                           current_price=entry * mult, current_multiple=mult,
                           realized_pnl_usd=3.0 * (mult - 1.0))

    # ---- live trade history: stops, rides, expired ------------------------ #
    hist = [
        ("DEMOH1", "TURBO", 0.665, "stopped", 1, 4.2),
        ("DEMOH2", "FROGG", 0.665, "stopped", 1, 9.8),
        ("DEMOH3", "NAPKIN", 5.61, "rode_to_horizon", 0, 51.0),
        ("DEMOH4", "CLAWZ", 0.665, "stopped", 1, 30.1),
        ("DEMOH5", "MILADY", 2.13, "rode_to_horizon", 0, 66.7),
        ("DEMOH6", "RUGME", 0.665, "stopped", 1, 90.5),
    ]
    for i, (mint, tick, mult, reason, stopped, hrs_ago) in enumerate(hist):
        t_exit = NOW - timedelta(hours=hrs_ago)
        t_ent = t_exit - timedelta(hours=rng.uniform(2, 20))
        entry = rng.uniform(2e-5, 4e-4)
        seen(mint, tick); signal(mint, tick, t_ent - timedelta(hours=2))
        pid = st.create_position(mint=mint, ticker=tick, signal_at=t_ent - timedelta(hours=2),
                                 signal_price=entry * 2, state="STOPPED" if stopped else "EXITED",
                                 t0_epoch=(t_ent - timedelta(hours=2)).timestamp())
        st.update_position(mint, entry_at=iso(t_ent), entry_price=entry, stake_usd=3.0,
                           realized_multiple=mult, current_multiple=mult,
                           realized_pnl_usd=3.0 * (mult - 1), closed_at=iso(t_exit),
                           close_reason=reason)
        st.record_close(position_id=pid, mint=mint, ticker=tick, entry_at=t_ent, entry_price=entry,
                        stake_usd=3.0, exit_at=t_exit, close_reason=reason, realized_multiple=mult,
                        pnl_usd=3.0 * (mult - 1), peak_multiple=mult * 1.4,
                        held_hours=(t_exit - t_ent).total_seconds() / 3600, n_tp=1 if mult > 1 else 0,
                        was_stopped=bool(stopped), was_secured=mult >= 3)
    for mint, tick, hrs in [("DEMOE1", "SLOTH", 70.0), ("DEMOE2", "YAWN", 55.0)]:
        t_sig = NOW - timedelta(hours=hrs + 48)
        seen(mint, tick); signal(mint, tick, t_sig)
        pid = st.create_position(mint=mint, ticker=tick, signal_at=t_sig, signal_price=1e-4,
                                 state="EXPIRED", t0_epoch=t_sig.timestamp())
        st.update_position(mint, closed_at=iso(t_sig + timedelta(hours=48)),
                           close_reason="no_dip_within_48h")

    # ---- shadow lab: forward race with real texture ------------------------ #
    cur = st.conn
    for i in range(1, 19):
        cid = f"C{i}"
        n = rng.randint(4, 14)
        for j in range(n):
            if cid == "C4":
                m = rng.choice([0.665, 0.31, 0.18, 2.1, 5.6])
            elif cid in ("C7", "C13"):
                m = rng.choice([0.55, 0.72, 0.665, 0.81, 1.4])
            elif cid == "C5":
                m = rng.choice([0.665, 0.665, 1.8, 6.2])
            else:
                m = rng.choice([0.665, 0.665, 0.665, 1.6, 2.4, 3.1])
            t_c = NOW - timedelta(hours=rng.uniform(2, 120))
            cur.execute("INSERT INTO shadow_trades(config_id,mint,ticker,entered_at,closed_at,"
                        "realized_multiple,close_reason) VALUES(?,?,?,?,?,?,?)",
                        (cid, f"DEMOS{i}{j}", f"S{i}{j}", iso(t_c - timedelta(hours=3)), iso(t_c),
                         m, "stopped" if m < 1 else "rode_to_horizon"))
        for j in range(rng.randint(0, 3)):
            snap = {"v": 2, "config_id": cid, "sig": 2e-4, "t0": NOW.timestamp() - 3600,
                    "ticker": f"O{i}{j}", "legs": [], "awaiting_target": None, "done": False,
                    "trail": None, "cur_entered_at": None,
                    "cur": {"state": "ENTERED", "sig": 2e-4, "t0": NOW.timestamp() - 3600,
                            "entry": 1e-4, "stop_price": 7e-5, "rem": 1.0, "pr": 0.0,
                            "n_tp": 0, "lvl": 3.0, "secured": False, "peak_price": 1.2e-4,
                            "low_price": None}}
            cur.execute("INSERT OR REPLACE INTO shadow_riders(config_id,mint,snapshot_json,state,"
                        "updated_at) VALUES(?,?,?,?,?)",
                        (cid, f"DEMOO{(i + j) % 3 + 1}", json.dumps(snap), "ENTERED", iso(NOW)))
    cur.execute("INSERT INTO research_runs(ts,status,verdict_json) VALUES(?,?,?)",
                (iso(NOW - timedelta(hours=9)), "ok", json.dumps({
                    "ts": iso(NOW - timedelta(hours=9)), "status": "ok", "n_tokens": 565,
                    "any_config_clears_gate": False, "recommendation": None,
                    "degradation_alert": False})))
    st.conn.commit()

    # ---- MANUAL desk demo: a manual position, resting orders, watchlist ---- #
    m_mint, m_tick = "DemoManual1111111111111111111111111111111111", "HODL"
    seen(m_mint, m_tick)
    mpid = st.create_position(mint=m_mint, ticker=m_tick, signal_at=NOW - timedelta(hours=5),
                              signal_price=1.0, state="ENTERED")
    st.update_position(m_mint, controller="manual", entry_at=iso(NOW - timedelta(hours=5)),
                       entry_price=1.0, stake_usd=5.0, tokens_qty=5.0, remaining_frac=1.0,
                       proceeds_units=0.0, current_price=2.4, current_multiple=2.4,
                       peak_price=2.8, realized_pnl_usd=7.0)
    st.append_event(position_id=mpid, mint=m_mint, ts=NOW - timedelta(hours=5),
                    event_type="ENTER", price=1.0, proceeds_usd=5.0, remaining_frac=1.0,
                    note="direct buy — algo rides it (config #1)")   # production books direct buys as ENTER
    st.create_order(mint=m_mint, ticker=m_tick, position_id=mpid, kind="take_profit", side="sell",
                    trigger_type="mult_at_or_above", trigger_value=5.0, size_kind="token_frac",
                    size_value=0.5, note="ride target")
    st.create_order(mint=m_mint, ticker=m_tick, position_id=mpid, kind="trailing_stop", side="sell",
                    trigger_type="peak_drawdown_pct", trigger_value=0.3, size_kind="token_frac",
                    size_value=1.0, hwm=2.8)
    w_mint, w_tick = "DemoWatch11111111111111111111111111111111111", "SNIPE"
    st.create_order(mint=w_mint, ticker=w_tick, kind="limit", side="buy",
                    trigger_type="price_at_or_below", trigger_value=0.0004, size_kind="usd",
                    size_value=4.0, note="dip entry")
    st.add_watch(w_mint, ticker=w_tick, note="watching for a dip")
    st.add_watch("DemoWatch22222222222222222222222222222222222", ticker="MOON")
    st.record_alert(severity="INFO", kind="MANUAL_FILL",
                    message="manual BUY HODL $5.00 @ 1.0", ts=NOW - timedelta(hours=5))
    st.conn.commit()

    # ---- alerts + live equity path ---------------------------------------- #
    st.record_alert(severity="WARN", kind="DRIFT", message="win% 12% vs expected 10% — inside tolerance band",
                    ts=NOW - timedelta(hours=3))
    st.record_alert(severity="INFO", kind="RESEARCH", message="weekly re-measurement complete: no change",
                    ts=NOW - timedelta(hours=9))
    bal = 500.0
    for h in range(72, 0, -2):
        bal += rng.uniform(-1.2, 1.5)
        st.sample_bankroll(ts=NOW - timedelta(hours=h), realized_equity_usd=round(bal, 2),
                           unrealized_equity_usd=round(bal + rng.uniform(-2, 8), 2),
                           deployed_usd=9.0, dry_powder_usd=round(bal - 9, 2), n_open=3,
                           n_watching=5, realized_pnl_cum_usd=round(bal - 500, 2))
    st.close()
    print(f"demo DB ready: {dst_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
