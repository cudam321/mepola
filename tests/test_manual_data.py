"""M6 — the manual read layer: manual_desk + manual-vs-algo attribution."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dashboard import data  # noqa: E402
from memebot.live.state import LiveState  # noqa: E402

NOW = datetime.now(timezone.utc)
MMINT = "MintManualData11111111111111111111111111111"
AMINT = "MintAlgoData1111111111111111111111111111111"


def _seed(db):
    st = LiveState(db)
    st.set_system("seeded_at", "2020-01-01T00:00:00+00:00")   # everything after is live
    # a live manual open position + an order on it + a watchlist entry
    st.create_position(mint=MMINT, ticker="MAN", signal_at=NOW, signal_price=1.0, state="ENTERED")
    st.update_position(MMINT, controller="manual", entry_price=1.0, stake_usd=5.0, tokens_qty=5.0,
                       remaining_frac=1.0, current_price=2.0, current_multiple=2.0)
    st.mark_seen(MMINT, outcome="positioned")
    st.create_order(mint=MMINT, ticker="MAN", kind="take_profit", side="sell",
                    trigger_type="price_at_or_above", trigger_value=3.0, size_kind="token_frac",
                    size_value=0.5)
    st.add_watch("MintWatch111111111111111111111111111111111", ticker="WCH")
    # a live algo CLOSED trade (win) + its position row (controller algo)
    pid = st.create_position(mint=AMINT, ticker="ALG", signal_at=NOW, signal_price=1.0, state="EXITED")
    st.update_position(AMINT, controller="algo")
    st.mark_seen(AMINT, outcome="positioned")
    st.record_close(position_id=pid, mint=AMINT, ticker="ALG", entry_at=NOW, entry_price=1.0,
                    stake_usd=3.0, exit_at=NOW, close_reason="rode_to_horizon",
                    realized_multiple=4.0, pnl_usd=9.0)
    st.close()


def test_manual_desk(tmp_path):
    db = tmp_path / "s.db"
    _seed(db)
    st = data.open_state(db)
    try:
        desk = data.manual_desk(st)
    finally:
        st.close()
    assert desk["n_open_orders"] == 1
    assert len(desk["orders"]) == 1 and desk["orders"][0]["kind"] == "take_profit"
    assert len(desk["watchlist"]) == 1
    assert len(desk["positions"]) == 1 and desk["positions"][0]["mint"] == MMINT
    assert desk["positions"][0]["n_open_orders"] == 1
    assert desk["manual_exposure_usd"] == 5.0
    assert desk["caps"]["manual_trade_hard_cap_usd"] == 10.0


def test_attribution_splits_manual_and_algo(tmp_path):
    db = tmp_path / "s.db"
    _seed(db)
    st = data.open_state(db)
    try:
        attr = data.attribution(st)
    finally:
        st.close()
    # algo: one closed 4x win, +$9 realized
    assert attr["algo"]["n"] == 1 and attr["algo"]["realized_pnl"] == 9.0
    assert attr["algo"]["best"] == 4.0
    # manual: open at 2x, +$5 unrealized (5 stake * (2-1)), nothing closed yet
    assert attr["manual"]["n"] == 0
    assert attr["manual"]["n_open"] == 1
    assert attr["manual"]["unrealized_pnl"] == 5.0


def test_snapshot_drops_dead_manual_payload(tmp_path):
    # audit #26: the manual/attribution payload is NOT pushed in the ~2s snapshot (no consumer);
    # manual state rides open_positions + meta.n_open_orders. The functions remain callable directly.
    db = tmp_path / "s.db"
    _seed(db)
    st = data.open_state(db)
    try:
        snap = data.snapshot(st)
    finally:
        st.close()
    assert "manual" not in snap and "attribution" not in snap
    assert snap["meta"]["n_open_orders"] == 1
    # the manual open position appears in the positions table with its controller tag
    man = [p for p in snap["positions"] if p["mint"] == MMINT]
    assert man and man[0]["controller"] == "manual"
