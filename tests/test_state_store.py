"""Round-trip + read-contract tests for the SQLite state store (no network)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from memebot.live.state import LiveState

T0 = datetime(2026, 6, 1, 0, 0, 0, tzinfo=timezone.utc)


def _db(tmp_path):
    return LiveState(tmp_path / "live_state.db")


def test_schema_and_system_defaults(tmp_path):
    st = _db(tmp_path)
    assert st.get_system("schema_version") == "1"
    assert st.get_system("mode") == "paper"
    assert st.get_system("kill_switch") == "off"
    st.close()


def test_seen_mint_dedup(tmp_path):
    st = _db(tmp_path)
    assert not st.is_seen("MINT1")
    st.mark_seen("MINT1", ticker="AAA", first_seen_at=T0)
    assert st.is_seen("MINT1")
    st.close()


def test_position_lifecycle_roundtrip(tmp_path):
    st = _db(tmp_path)
    pid = st.create_position(mint="MINT1", ticker="AAA", signal_at=T0, signal_price=100.0,
                             state="WATCHING", dip_deadline=T0 + timedelta(hours=48))
    st.update_position("MINT1", state="ENTERED", entry_price=50.5, stake_usd=3.0,
                       tokens_qty=3.0 / 50.5, stop_price=0.7 * 50.5, remaining_frac=1.0)
    st.append_event(position_id=pid, mint="MINT1", ts=T0, event_type="ENTER", price=50.5,
                    remaining_frac=1.0, note="dip filled")
    pos = st.get_position("MINT1")
    assert pos["state"] == "ENTERED" and pos["entry_price"] == 50.5
    assert len(st.active_positions()) == 1
    assert len(st.events_for("MINT1")) == 1

    # close it as a winner
    st.update_position("MINT1", state="EXITED", current_multiple=197.6, realized_multiple=197.6,
                       closed_at=None, close_reason="rode_to_horizon")
    st.record_close(position_id=pid, mint="MINT1", ticker="AAA", entry_at=T0, entry_price=50.5,
                    stake_usd=3.0, exit_at=T0 + timedelta(days=14), close_reason="rode_to_horizon",
                    realized_multiple=197.6, pnl_usd=3.0 * (197.6 - 1), n_tp=5, was_secured=True)
    assert len(st.active_positions()) == 0
    closed = st.closed_trades()
    assert len(closed) == 1 and abs(closed[0]["realized_multiple"] - 197.6) < 1e-9
    st.close()


def test_v_multiples_includes_realized_and_unrealized(tmp_path):
    st = _db(tmp_path)
    # one live (unrealized) position
    st.create_position(mint="LIVE", ticker="L", signal_at=T0, signal_price=1.0, state="RIDING")
    st.update_position("LIVE", current_multiple=12.5)
    # one closed (realized) trade
    pid = st.create_position(mint="DEAD", ticker="D", signal_at=T0, signal_price=1.0, state="STOPPED")
    st.record_close(position_id=pid, mint="DEAD", ticker="D", entry_at=T0, entry_price=1.0,
                    stake_usd=3.0, exit_at=T0, close_reason="stopped", realized_multiple=0.665,
                    pnl_usd=3.0 * (0.665 - 1), was_stopped=True)
    mults = {m["mint"]: (m["multiple"], m["kind"]) for m in st.multiples()}
    assert mults["LIVE"] == (12.5, "unrealized")
    assert abs(mults["DEAD"][0] - 0.665) < 1e-9 and mults["DEAD"][1] == "realized"
    st.close()


def test_read_only_open(tmp_path):
    st = _db(tmp_path)
    st.mark_seen("MINT1", first_seen_at=T0)
    st.close()
    ro = LiveState(tmp_path / "live_state.db", read_only=True)
    assert ro.is_seen("MINT1")
    ro.close()
