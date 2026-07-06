"""Risk governor tests (no network)."""

from __future__ import annotations

from memebot.live.risk import STAKE_HARD_CAP_USD, RiskConfig, RiskGovernor
from memebot.live.state import LiveState, utcnow

CFG = RiskConfig(bankroll_usd=500.0, stake_usd=3.0, max_concurrent=25,
                 total_deployed_cap_usd=200.0, daily_loss_cap_usd=50.0)


def _ev(**over):
    base = dict(mode="live", kill=False, n_active=0, deployed_usd=0.0,
                prospective_stake=3.0, daily_loss_usd=0.0, cfg=CFG)
    base.update(over)
    return RiskGovernor.evaluate(**base)


def test_sizing_fixed_and_fraction():
    g = RiskGovernor(state=None, cfg=CFG)
    assert g.size_for() == 3.0
    gf = RiskGovernor(state=None, cfg=RiskConfig(stake_mode="fraction", stake_fraction=0.005))
    assert gf.size_for(500.0) == 2.5


def test_kill_switch_blocks():
    assert _ev(kill=True).reason == "kill"


def test_daily_loss_blocks_live_only():
    # audit #14: the daily-loss cap is a LIVE safety limit. In paper it must NOT bind — a paper
    # daily-loss kill would permanently halt the 24/7 take-every-call measurement.
    assert _ev(mode="paper", daily_loss_usd=50.0).ok
    assert _ev(mode="live", daily_loss_usd=60.0).reason == "daily_loss"


def test_paper_is_uncapped():
    # paper mirrors the backtest: concurrency / deployed caps do NOT bind
    d = _ev(mode="paper", n_active=9999, deployed_usd=1e9, prospective_stake=3.0)
    assert d.ok and d.reason is None


def test_live_concurrency_cap():
    assert _ev(mode="live", n_active=25).reason == "max_concurrent"
    assert _ev(mode="live", n_active=24).ok


def test_live_deployed_cap():
    assert _ev(mode="live", deployed_usd=199.0, prospective_stake=3.0).reason == "deployed_cap"
    assert _ev(mode="live", deployed_usd=190.0, prospective_stake=3.0).ok


def test_daily_loss_accrual_trips_kill(tmp_path):
    st = LiveState(tmp_path / "s.db")
    st.set_system("mode", "live")
    g = RiskGovernor(st, RiskConfig(daily_loss_cap_usd=10.0))
    g.record_realized_pnl(-4.0)
    assert not g.kill_on
    g.record_realized_pnl(-7.0)   # cumulative 11 >= cap 10
    assert g.kill_on
    assert g.can_enter().reason == "kill"
    st.close()


def test_can_enter_reads_state(tmp_path):
    st = LiveState(tmp_path / "s.db")
    st.set_system("mode", "live")
    g = RiskGovernor(st, CFG)
    # no positions -> ok
    assert g.can_enter(prospective_stake=3.0).ok
    st.close()


# -- runtime overrides (system_state ctl_* keys, set by the dashboard control API) -- #

def test_runtime_stake_override_and_hard_cap(tmp_path):
    st = LiveState(tmp_path / "s.db")
    g = RiskGovernor(st, CFG)
    assert g.size_for() == 3.0                         # no override -> config value
    st.set_system("ctl_stake_usd", "5")
    assert g.size_for() == 5.0                         # override applies
    st.set_system("ctl_stake_usd", "25")
    assert g.size_for() == STAKE_HARD_CAP_USD == 10.0  # stage39: $25-fixed busts -> clamp
    st.close()


def test_runtime_max_concurrent_override_live(tmp_path):
    st = LiveState(tmp_path / "s.db")
    st.set_system("mode", "live")
    st.set_system("ctl_max_concurrent", "2")
    g = RiskGovernor(st, CFG)                          # cfg says 25; override says 2
    for i in range(2):
        st.create_position(mint=f"MINT{i}", ticker=f"T{i}", signal_at=utcnow(),
                           signal_price=1.0, state="ENTERED")
        st.update_position(f"MINT{i}", stake_usd=3.0)
    assert g.can_enter().reason == "max_concurrent"
    st.close()


def test_runtime_daily_loss_cap_override(tmp_path):
    st = LiveState(tmp_path / "s.db")
    st.set_system("mode", "live")                     # daily-loss kill is LIVE-only (audit #14)
    g = RiskGovernor(st, RiskConfig(daily_loss_cap_usd=50.0))
    st.set_system("ctl_daily_loss_cap_usd", "5")
    g.record_realized_pnl(-6.0)                        # breaches the overridden $5 cap
    assert g.kill_on
    st.close()


def test_daily_loss_does_not_trip_kill_in_paper(tmp_path):
    # audit #14: accrue the loss for observability but NEVER trip the sticky kill in paper
    st = LiveState(tmp_path / "s.db")
    st.set_system("mode", "paper")
    g = RiskGovernor(st, RiskConfig(daily_loss_cap_usd=5.0))
    g.record_realized_pnl(-9.0)                        # would breach in live
    assert not g.kill_on
    assert g.can_enter().ok                            # paper stays uncapped
    st.close()
