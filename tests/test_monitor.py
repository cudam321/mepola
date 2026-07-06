"""Monitor tests — self-awareness: bleeding as designed vs broken (no network)."""

from __future__ import annotations

from datetime import timedelta

from memebot.live.monitor import Monitor, build_expectation
from memebot.live.state import LiveState, to_iso, utcnow

# A config-#1-like distribution: mostly ~0.665x bleed, a few winners, one big tail.
DIST = [0.665] * 90 + [1.5, 2.2, 4.0, 8.0, 12.0] + [1.0] * 4 + [197.6]


def test_build_expectation_shape():
    exp = build_expectation(DIST)
    assert exp.n == len(DIST)
    assert 0.0 < exp.win_rate < 0.2          # lottery: low win rate
    assert exp.ex_tail_mean < exp.mean       # dropping the tail lowers the mean
    assert exp.ci_lo <= exp.mean <= exp.ci_hi


def test_assess_in_band_is_as_designed():
    exp = build_expectation(DIST)
    m = Monitor(state=None, expectation=exp)
    a = m.assess(DIST)                        # same distribution -> in band
    assert a.status == "as_designed"


def test_assess_out_of_band_flags_drift():
    exp = build_expectation(DIST)
    m = Monitor(state=None, expectation=exp)
    # a suspiciously winning live sample -> off expectation
    a = m.assess([2.0, 3.0, 5.0, 4.0, 6.0, 2.5, 3.5, 8.0])
    assert a.status == "off_expectation"
    assert a.reasons


def test_feed_outage_detection(tmp_path):
    st = LiveState(tmp_path / "s.db")
    m = Monitor(st, build_expectation(DIST), feed_max_gap_s=1800)
    now = utcnow()
    st.set_system("last_feed_ok_ts", to_iso(now - timedelta(minutes=60)))
    alert = m.check_feed(now=now)
    assert alert and alert["kind"] == "FEED_OUTAGE"
    # fresh heartbeat clears it
    m.heartbeat(now=now)
    assert m.check_feed(now=now) is None
    st.close()


def test_listener_staleness_detection(tmp_path):
    # audit #9: a wedged listener (stale heartbeat) must alert; a fresh stamp / never-connected must not
    st = LiveState(tmp_path / "s.db")
    m = Monitor(st, build_expectation(DIST), listener_max_gap_s=600)
    now = utcnow()
    assert m.check_listener(now=now) is None                      # never connected -> no false alarm
    st.set_system("last_listener_ok_ts", to_iso(now - timedelta(minutes=30)))
    alert = m.check_listener(now=now)
    assert alert and alert["kind"] == "LISTENER_STALE"
    st.set_system("last_listener_ok_ts", to_iso(now - timedelta(seconds=10)))  # fresh 45s heartbeat
    assert m.check_listener(now=now) is None
    st.close()


def test_path_deviation():
    assert Monitor.path_deviation(197.60, 197.60) is None
    dev = Monitor.path_deviation(150.0, 197.60)
    assert dev and dev["kind"] == "PATH_DEVIATION"


def test_from_closed_trades_builds_expectation(tmp_path):
    st = LiveState(tmp_path / "s.db")
    for i, m in enumerate(DIST):
        pid = st.create_position(mint=f"M{i}", ticker="A", signal_at=utcnow(), signal_price=1.0,
                                 state="EXITED")
        st.record_close(position_id=pid, mint=f"M{i}", ticker="A", entry_at=utcnow(), entry_price=1.0,
                        stake_usd=3.0, exit_at=utcnow(), close_reason="x", realized_multiple=m,
                        pnl_usd=3 * (m - 1))
    mon = Monitor.from_closed_trades(st)
    assert mon.exp.n == len(DIST)      # no seen_mints rows -> falls back to all closed trades
    st.close()


def _add_closed(st, mint, m, outcome):
    st.mark_seen(mint, outcome=outcome)
    pid = st.create_position(mint=mint, ticker="A", signal_at=utcnow(), signal_price=1.0,
                             state="EXITED")
    st.record_close(position_id=pid, mint=mint, ticker="A", entry_at=utcnow(), entry_price=1.0,
                    stake_usd=3.0, exit_at=utcnow(), close_reason="x", realized_multiple=m,
                    pnl_usd=3 * (m - 1))


def test_run_once_judges_live_against_frozen_seed_band_and_dedups(tmp_path):
    """F32: the band is frozen on the SEED (backtest) distribution; run_once assesses only
    LIVE-provenance trades against it, and re-alerts only on a status TRANSITION."""
    st = LiveState(tmp_path / "s.db")
    for i, m in enumerate(DIST):                     # seed replay (outcome='seen')
        _add_closed(st, f"S{i}", m, "seen")
    mon = Monitor.from_closed_trades(st)
    assert mon.exp.n == len(DIST)                    # band built from seed rows ONLY

    # a suspiciously WINNING live sample (outcome='positioned') -> off_expectation
    for i, m in enumerate([2.0, 3.0, 5.0, 4.0, 6.0, 2.5]):
        _add_closed(st, f"L{i}", m, "positioned")
    a1 = mon.run_once()
    assert a1.status == "off_expectation"
    drift = [al for al in st.recent_alerts() if al["kind"] == "DRIFT"]
    assert len(drift) == 1
    # a second pass with the SAME status must NOT re-alert (dedup by transition)
    mon.run_once()
    assert len([al for al in st.recent_alerts() if al["kind"] == "DRIFT"]) == 1
    st.close()
