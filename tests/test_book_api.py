"""The LIVE/PAPER book toggle: read endpoints resolve `book` to the right DB (offline)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import dashboard.server.app as appmod  # noqa: E402
from memebot.live.state import LiveState  # noqa: E402


@pytest.fixture()
def books(tmp_path, monkeypatch):
    live_db, paper_db = tmp_path / "live.db", tmp_path / "paper.db"
    st = LiveState(live_db)
    st.set_system("mode", "live")
    st.set_system("bankroll_start_usd", "23.8")
    st.close()
    st = LiveState(paper_db)
    st.set_system("mode", "paper")
    st.set_system("bankroll_start_usd", "500")
    st.close()
    monkeypatch.setattr(appmod, "DB_PATH", str(live_db))
    monkeypatch.setenv("MEMEBOT_PAPER_DB", str(paper_db))
    with TestClient(appmod.app) as c:
        yield c


def test_snapshot_book_switches_db(books):
    live = books.get("/api/snapshot").json()
    paper = books.get("/api/snapshot?book=paper").json()
    assert live["meta"]["book"] == "live" and live["meta"]["mode"] == "live"
    assert live["meta"]["bankroll_start_usd"] == 23.8
    assert paper["meta"]["book"] == "paper" and paper["meta"]["mode"] == "paper"
    assert paper["meta"]["bankroll_start_usd"] == 500.0


def test_history_stream_token_lab_accept_book(books):
    assert books.get("/api/history?book=paper").status_code == 200
    assert books.get("/api/stream?book=paper").status_code == 200
    # unknown mint/config in the paper book: the book resolves, the lookup itself 404s
    assert books.get("/api/token/MintBookApi111111111111111111111111111111?book=paper").status_code == 404
    assert books.get("/api/lab/c1?book=paper").status_code == 404


def test_missing_paper_book_404_and_bad_book_422(books, monkeypatch, tmp_path):
    monkeypatch.setenv("MEMEBOT_PAPER_DB", str(tmp_path / "nope.db"))
    assert books.get("/api/snapshot?book=paper").status_code == 404
    assert books.get("/api/snapshot?book=weird").status_code == 422


def test_paper_practice_order_lands_in_paper_db(books, tmp_path, monkeypatch):
    # PAPER practice desk: a book:"paper" order row lands in the paper DB (the twin fills it),
    # and the LIVE book never sees it — full functionality, zero real-money surface.
    import os
    M = "MintPracticeBook1111111111111111111111111111"
    r = books.post("/api/manual/order", json={
        "mint": M, "side": "buy", "kind": "market", "size_kind": "usd", "size_value": 3.0,
        "book": "paper"})
    assert r.status_code == 200, r.text
    st = LiveState(os.environ["MEMEBOT_PAPER_DB"])
    rows = st.open_orders(M)
    st.close()
    assert len(rows) == 1 and rows[0]["side"] == "buy"
    st = LiveState(appmod.DB_PATH)
    assert st.open_orders(M) == []
    st.close()


def test_paper_signal_lands_in_paper_db(books, monkeypatch):
    import os
    M = "MintPracticeSig22222222222222222222222222222"

    class FakePrice:
        def price_full(self, mints):
            return {M: {"usdPrice": 1.5}}

    monkeypatch.setattr(appmod, "_price_client", FakePrice())
    r = books.post("/api/signal", json={"mint": M, "ticker": "PRAC", "book": "paper"})
    assert r.status_code == 200, r.text
    st = LiveState(os.environ["MEMEBOT_PAPER_DB"])
    pend = st.pending_manual_signals()
    st.close()
    assert len(pend) == 1 and pend[0]["mint"] == M


def test_cancel_respects_book_never_crosses_id_overlap(books):
    # order ids autoincrement from 1 in BOTH DBs — a paper-view cancel of id=1 must NEVER touch the
    # live order id=1. This is the sharp cross-book routing invariant.
    import os
    live = LiveState(appmod.DB_PATH)
    live_oid = live.create_order(mint="MintLiveOrd11111111111111111111111111111111", kind="market",
                                 side="buy", trigger_type="now", trigger_value=None,
                                 size_kind="usd", size_value=3.0)
    live.close()
    paper = LiveState(os.environ["MEMEBOT_PAPER_DB"])
    paper_oid = paper.create_order(mint="MintPaperOrd1111111111111111111111111111111", kind="market",
                                   side="buy", trigger_type="now", trigger_value=None,
                                   size_kind="usd", size_value=3.0)
    paper.close()
    assert live_oid == paper_oid == 1                         # the overlap that makes this dangerous
    # cancel id=1 on the PAPER book
    assert books.delete(f"/api/manual/order/{paper_oid}?book=paper").status_code == 200
    live = LiveState(appmod.DB_PATH)
    assert live.get_order(live_oid)["status"] == "open"       # LIVE #1 untouched
    live.close()
    paper = LiveState(os.environ["MEMEBOT_PAPER_DB"])
    assert paper.get_order(paper_oid)["status"] == "cancelled"
    paper.close()


def test_meta_carries_wallet_truth(books, tmp_path, monkeypatch):
    # the live book surfaces the engine-written on-chain wallet fields
    db = tmp_path / "live2.db"
    st = LiveState(db)
    st.set_system("wallet_sol", "0.297594")
    st.set_system("wallet_usd", "23.81")
    st.set_system("wallet_at", "2026-07-06T12:00:00+00:00")
    st.close()
    monkeypatch.setattr(appmod, "DB_PATH", str(db))
    m = books.get("/api/snapshot").json()["meta"]
    assert m["wallet_sol"] == pytest.approx(0.297594)
    assert m["wallet_usd"] == pytest.approx(23.81)
    assert m["wallet_at"] == "2026-07-06T12:00:00+00:00"
