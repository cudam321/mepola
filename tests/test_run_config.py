"""Config loading + orchestrator wiring imports (no network)."""

from __future__ import annotations

from memebot.config import Settings
from memebot.live.run import load_configs


def test_load_configs_reads_tailrider_section():
    cfg, risk, mode = load_configs(Settings.load())
    # config #1 locked values
    assert cfg.dip_trigger == 0.50
    assert cfg.stop_level_mult == 0.70     # -30% stop (NOT -70%)
    assert cfg.tp1_mult == 3.0
    assert cfg.tp1_sell_frac == 0.33
    # decided sizing
    assert risk.stake_mode == "fixed"
    assert risk.stake_usd == 3.0
    assert risk.bankroll_usd == 500.0
    assert mode in ("paper", "live")


def test_defaults_when_section_missing():
    # a bare Settings with no [strategy.tailrider] still yields config #1 defaults
    s = Settings.load()
    s.raw.pop("strategy", None)
    cfg, risk, mode = load_configs(s)
    assert cfg.stop_level_mult == 0.70
    assert risk.stake_usd == 3.0
    assert mode == "paper"


def test_repair_orphan_closed_trades_reconstructs_missing_row(tmp_path):
    # audit #13: a crash between the two close commits leaves an EXITED position with NO closed_trades
    # row (and possibly a NULL realized_multiple) -> repair reconstructs it from the summed sell proceeds.
    from memebot.live.run import repair_orphan_closed_trades
    from memebot.live.state import LiveState, utcnow
    M = "MintRepairTest111111111111111111111111111111"
    st = LiveState(tmp_path / "s.db")
    now = utcnow()
    pid = st.create_position(mint=M, ticker="TOK", signal_at=now, signal_price=1.0,
                             state="WATCHING", t0_epoch=now.timestamp())
    # closed EXITED with real entry/stake but realized_multiple left NULL (the crash sub-window)
    st.update_position(M, state="EXITED", entry_price=1.0, stake_usd=3.0, closed_at=now.isoformat())
    st.append_event(position_id=pid, mint=M, ts=now, event_type="TP", price=3.0, frac=0.33,
                    proceeds_usd=6.0, remaining_frac=0.67)
    st.append_event(position_id=pid, mint=M, ts=now, event_type="FINALIZE", price=1.0, frac=0.67,
                    proceeds_usd=3.0, remaining_frac=0.0)
    assert st.query("SELECT id FROM closed_trades WHERE position_id=?", (pid,)) == []
    assert repair_orphan_closed_trades(st) == 1
    ct = st.query("SELECT realized_multiple AS m, pnl_usd AS p FROM closed_trades WHERE position_id=?", (pid,))
    assert len(ct) == 1
    assert abs(ct[0]["p"] - 6.0) < 1e-6          # 9 proceeds - 3 stake
    assert abs(ct[0]["m"] - 3.0) < 1e-6          # 9 / 3
    assert repair_orphan_closed_trades(st) == 0  # idempotent — no duplicate row
    st.close()
