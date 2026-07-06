"""PAPER TWIN — in live mode the paper measurement machine keeps trading inside the same
orchestrator (same listener/feed, its own DB), and the live bankroll boot-write respects the
real-wallet anchor. Offline: the orchestrator is constructed but no loops are run."""

from __future__ import annotations

from datetime import timedelta

from memebot.config import Settings
from memebot.live.run import Orchestrator
from memebot.live.state import LiveState, utcnow
from memebot.models import Signal, SignalSide

MINT = "MintPaperTwin1111111111111111111111111111111"


def _live_settings():
    s = Settings.load()
    s.raw.setdefault("strategy", {}).setdefault("tailrider", {})["mode"] = "live"
    return s


def test_paper_twin_trades_alongside_live(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMEBOT_PAPER_DB", str(tmp_path / "paper.db"))
    monkeypatch.delenv("MEMEBOT_LIVE_ARMED", raising=False)
    monkeypatch.delenv("MEMEBOT_LIVE_SEND", raising=False)
    orch = Orchestrator(tmp_path / "live.db", settings=_live_settings())
    assert orch.paper_eng is not None
    assert orch.paper_state.get_system("mode") == "paper"
    # a call in the paper book: a -50% dip tick ENTERs it (uncapped measurement),
    # while the live book stays untouched
    t0 = utcnow()
    sig = Signal(source_channel="test", message_id=1, posted_at=t0, raw_text="call",
                 side=SignalSide.BUY, mint=MINT, ticker="TWIN", parse_confidence=1.0)
    assert orch.paper_eng.ingest_call(sig, price=1.0, now=t0)
    orch._on_tick(MINT, 0.45, t0 + timedelta(seconds=1))
    assert orch.paper_state.get_position(MINT)["state"] == "ENTERED"
    assert orch.state.get_position(MINT) is None
    assert orch._mint_needed(MINT)          # the twin keeps the mint tracked


def test_paper_orderbook_fires_practice_market_buy(tmp_path, monkeypatch):
    # the PAPER practice desk: an order row in the paper DB fires through the twin on the next tick
    # (PaperExecutor inline — simulated fill), and the live book never sees it
    M2 = "MintPaperTwin2222222222222222222222222222222"
    monkeypatch.setenv("MEMEBOT_PAPER_DB", str(tmp_path / "paper.db"))
    orch = Orchestrator(tmp_path / "live.db", settings=_live_settings())
    assert orch.paper_orderbook is not None
    orch.paper_state.create_order(mint=M2, ticker="PRAC", kind="market", side="buy",
                                  trigger_type="now", trigger_value=None,
                                  size_kind="usd", size_value=3.0)
    orch._on_tick(M2, 2.0, utcnow())
    pos = orch.paper_state.get_position(M2)
    assert pos and pos["state"] == "ENTERED" and pos["controller"] == "algo"
    assert orch.state.get_position(M2) is None       # live book untouched


def test_research_ghost_cleanup_and_no_boot_autofire(tmp_path, monkeypatch):
    # user report "measurement running 92 min": (a) a status='running' research row at boot is a DEAD
    # run (in-process thread) -> marked failed; (b) a fresh book must not auto-fire the weekly
    # re-measure at boot -> the clock is seeded instead.
    monkeypatch.setenv("MEMEBOT_PAPER_DB", str(tmp_path / "paper.db"))
    db = tmp_path / "live.db"
    st = LiveState(db)
    st.conn.execute("INSERT INTO research_runs(ts,status,verdict_json) VALUES(?,?,?)",
                    ("2026-07-06T11:06:09+00:00", "running", "{}"))
    st.conn.commit()
    st.close()
    orch = Orchestrator(db, settings=_live_settings())
    rows = orch.state.query("SELECT status FROM research_runs")
    assert rows and rows[0]["status"] == "failed"                    # ghost clock stopped
    assert orch.state.get_system("last_research_at") is not None    # weekly re-measure NOT boot-fired


def test_twin_refuses_to_open_the_live_db(tmp_path, monkeypatch):
    # HARD GUARD: MEMEBOT_PAPER_DB == live DB would flip the LIVE DB to mode='paper' (real sends
    # refused while bags are open). The twin must disable itself, and the live DB stays mode=live.
    db = tmp_path / "live.db"
    monkeypatch.setenv("MEMEBOT_PAPER_DB", str(db))
    orch = Orchestrator(db, settings=_live_settings())
    assert orch.paper_eng is None and orch.paper_state is None
    assert orch.state.get_system("mode") == "live"      # NOT flipped to paper


def test_wallet_anchor_guards_zero_and_deployed(tmp_path, monkeypatch):
    import asyncio
    monkeypatch.setenv("MEMEBOT_PAPER_DB", str(tmp_path / "paper.db"))
    monkeypatch.setenv("MEMEBOT_LIVE_ARMED", "1")
    monkeypatch.setenv("MEMEBOT_LIVE_SEND", "1")
    orch = Orchestrator(tmp_path / "live.db", settings=_live_settings())
    # (a) a $0 read must NOT anchor (funding later would never re-anchor)
    monkeypatch.setattr(orch, "_wallet_read", lambda: (0.0, 0.0))
    asyncio.run(orch._refresh_wallet())
    assert orch.state.get_system("live_bankroll_anchored") is None
    # (b) never anchor while capital is deployed (would understate the account)
    from memebot.live.state import utcnow
    orch.state.create_position(mint="MintDeployed1111111111111111111111111111111", ticker="D",
                               signal_at=utcnow(), signal_price=1.0, state="ENTERED")
    orch.state.update_position("MintDeployed1111111111111111111111111111111", stake_usd=3.0)
    monkeypatch.setattr(orch, "_wallet_read", lambda: (0.2976, 23.9))
    asyncio.run(orch._refresh_wallet())
    assert orch.state.get_system("live_bankroll_anchored") is None
    assert orch.state.get_system("wallet_usd") == "23.90"     # display line still refreshed


def test_paper_twin_absent_in_paper_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMEBOT_PAPER_DB", str(tmp_path / "paper.db"))
    s = Settings.load()
    s.raw.setdefault("strategy", {}).setdefault("tailrider", {})["mode"] = "paper"
    orch = Orchestrator(tmp_path / "live.db", settings=s)
    assert orch.paper_eng is None           # the main engine IS the paper machine


def test_bankroll_boot_write_respects_wallet_anchor(tmp_path, monkeypatch):
    # once the live book is anchored to the REAL wallet, a reboot must NOT reset it to the $500
    # config fiction (user report 2026-07-06: "the balance says 500")
    monkeypatch.setenv("MEMEBOT_PAPER_DB", str(tmp_path / "paper.db"))
    db = tmp_path / "live.db"
    st = LiveState(db)
    st.set_system("live_bankroll_anchored", "2026-07-06T11:00:00+00:00")
    st.set_system("bankroll_start_usd", "23.85")
    st.close()
    orch = Orchestrator(db, settings=_live_settings())
    assert orch.state.get_system("bankroll_start_usd") == "23.85"
