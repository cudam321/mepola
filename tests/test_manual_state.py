"""M1 — manual-layer state store: orders/watchlist tables, positions.controller, caps.

Fully offline; a temp SQLite file per test. Verifies additive migrations are idempotent and the
CRUD round-trips, so nothing here can touch the pinned algo path."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from memebot.live.state import LiveState

NOW = datetime.now(timezone.utc)
MINT = "MintManual1111111111111111111111111111111111"


def test_manual_tables_and_columns_exist(tmp_path):
    st = LiveState(tmp_path / "s.db")
    try:
        # controller column defaults to 'algo' for a normally-created position
        pid = st.create_position(mint=MINT, ticker="TOK", signal_at=NOW, signal_price=1.0,
                                 state="WATCHING")
        pos = st.get_position(MINT)
        assert pos["controller"] == "algo"
        st.update_position(MINT, controller="manual")
        assert st.get_position(MINT)["controller"] == "manual"
        # caps seeded
        assert float(st.get_system("manual_cap_usd")) == 50.0
        assert float(st.get_system("manual_trade_hard_cap_usd")) == 10.0
        assert pid > 0
    finally:
        st.close()


def test_migration_idempotent_on_reopen(tmp_path):
    db = tmp_path / "s.db"
    LiveState(db).close()
    # reopening must not raise (ALTER guarded) and must preserve data
    st = LiveState(db)
    try:
        st.add_watch(MINT, ticker="TOK")
        assert st.is_watched(MINT)
    finally:
        st.close()
    st2 = LiveState(db)
    try:
        assert st2.is_watched(MINT)          # survived the second open
    finally:
        st2.close()


def test_order_crud_round_trip(tmp_path):
    st = LiveState(tmp_path / "s.db")
    try:
        oid = st.create_order(mint=MINT, ticker="TOK", kind="limit", side="buy",
                              trigger_type="price_at_or_below", trigger_value=0.5,
                              size_kind="usd", size_value=3.0,
                              expires_at=NOW + timedelta(hours=48))
        o = st.get_order(oid)
        assert o["status"] == "open" and o["kind"] == "limit" and o["side"] == "buy"
        assert o["trigger_value"] == 0.5 and o["size_value"] == 3.0
        assert o["expires_at"] is not None
        # open_orders sees it (globally + per mint)
        assert len(st.open_orders()) == 1
        assert len(st.open_orders(MINT)) == 1
        assert st.mints_with_open_orders() == [MINT]
        # transition to filled -> drops out of the actionable set
        st.update_order(oid, status="filled", filled_at=NOW.isoformat())
        assert st.open_orders() == []
        assert st.get_order(oid)["status"] == "filled"
        assert len(st.orders_for(MINT)) == 1     # history keeps it
    finally:
        st.close()


def test_watchlist_crud(tmp_path):
    st = LiveState(tmp_path / "s.db")
    try:
        st.add_watch(MINT, ticker="TOK", note="watch me")
        assert st.is_watched(MINT)
        wl = st.watchlist()
        assert len(wl) == 1 and wl[0]["ticker"] == "TOK" and wl[0]["note"] == "watch me"
        # upsert keeps ticker when re-added without one
        st.add_watch(MINT, note="new note")
        assert st.watchlist()[0]["ticker"] == "TOK"
        st.remove_watch(MINT)
        assert not st.is_watched(MINT) and st.watchlist() == []
    finally:
        st.close()
