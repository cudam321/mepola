"""research.run_remeasurement — the automated re-measurement, fully offline.

Synthetic universe: 10 tokens (one 200x-ish moonshot in TRAIN, the rest fast losers) priced
by a fake client. Pins: verdict structure, the gate's behaviour (an always-2x record clears;
a lottery-shaped record does not — the tail is refused as the whole edge), persistence of
research_runs / last_research_at, and that a poisoned price client yields a status='failed'
row without raising.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from memebot.live.research import gate_pass, run_remeasurement
from memebot.live.state import LiveState
from memebot.models import Candle, PriceSeries

BASE = datetime(2026, 6, 1, 0, 0, 0, tzinfo=timezone.utc)
NOW = datetime(2026, 6, 25, 0, 0, 0, tzinfo=timezone.utc)

# hourly (o, h, l, c) paths; sig = first open = 100
LOSER = [(100, 100, 100, 100), (95, 96, 45, 50), (45, 46, 20, 25), (20, 21, 3, 4)]
MOON = [(100, 100, 100, 100), (95, 100, 48, 55), (60, 70, 55, 65),
        (70, 50.5 * 250, 60, 50.5 * 200)]


def _series(mint, t0, path):
    candles = [Candle(ts=t0 + timedelta(hours=i), open=o, high=h, low=l, close=cl, volume=1.0)
               for i, (o, h, l, cl) in enumerate(path)]
    return PriceSeries(mint=mint, pool=None, timeframe="minute", aggregate=1, candles=candles)


def _build_universe(n=10):
    """n daily first-calls; the moonshot lands in TRAIN (day 2), the OOS tail is pure bleed."""
    series, msgs = {}, []
    for i in range(n):
        mint = f"MINT{i:02d}" + "x" * 30
        t0 = BASE + timedelta(days=i)
        series[mint] = _series(mint, t0, MOON if i == 2 else LOSER)
        msgs.append({"id": i + 1, "date": int(t0.timestamp()), "text": f"ape MINT{i}",
                     "mint": mint, "side": "buy"})
    return series, {"channel": "@test", "title": "test", "messages": msgs}


class FakeClient:
    def __init__(self, series_by_mint):
        self.series = series_by_mint

    def get_price_series(self, mint, start, end):
        s = self.series[mint]
        return PriceSeries(mint=mint, pool=None, timeframe=s.timeframe, aggregate=1,
                           candles=[c for c in s.candles if start <= c.ts <= end])


class PoisonClient:
    def get_price_series(self, mint, start, end):
        raise RuntimeError("boom")


def test_verdict_structure_no_config_clears_and_persistence(tmp_path):
    series, corpus = _build_universe()
    corpus_path = tmp_path / "corpus.json"
    corpus_path.write_text(json.dumps(corpus))
    st = LiveState(tmp_path / "s.db")
    v = run_remeasurement(st, corpus_path=corpus_path, cache_dir=tmp_path / "cache",
                          now=NOW, client=FakeClient(series), refresh_corpus=False, min_n=2)
    for key in ("ts", "status", "n_tokens", "n_new_priced", "n_skipped_unpriced", "champion",
                "top_configs", "any_config_clears_gate", "recommendation", "degradation_alert"):
        assert key in v, f"verdict missing {key}"
    assert v["status"] == "stale_corpus"            # no refresh -> honestly labeled
    assert v["n_tokens"] == 10
    assert v["n_new_priced"] == 10                  # empty cache, all within the fetch budget
    assert v["n_skipped_unpriced"] == 0
    # the lottery set does NOT clear the full gate; the honest verdict is "no change"
    assert v["any_config_clears_gate"] is False
    assert v["recommendation"] is None
    assert isinstance(v["degradation_alert"], bool)
    assert v["champion"]["cfg"] == {"dip": 0.5, "sl": 0.7, "ftp": 3.0, "fsell": 0.33,
                                    "reentry": None}
    assert v["champion"]["oos"] is not None and v["champion"]["oos"]["n"] >= 2
    assert v["champion"]["oos"]["mean"] < 1         # OOS is the pure bleed, as designed
    assert len(v["top_configs"]) == 8
    for entry in v["top_configs"]:
        assert set(entry) == {"cfg", "train", "oos", "clears"}
    # persisted: research_runs row + last_research_at
    run = st.latest_research_run()
    assert run is not None and run["status"] == "stale_corpus"
    assert json.loads(run["verdict_json"])["n_tokens"] == 10
    assert st.get_system("last_research_at") is not None
    st.close()


def test_gate_clears_a_fabricated_always_2x_record():
    ok, stats = gate_pass([2.0] * 40, list(range(40)))
    assert ok and stats["clears"]
    assert stats["ci_lo"] > 1 and stats["drop3"] > 1
    assert stats["f2_logG"] > 0 and stats["bank_500"] > 500


def test_gate_rejects_a_lottery_shaped_record():
    # real tail, terrible body — the exact shape that printed six false GOs
    mults = [0.0] * 90 + [50.0, 60.0]
    ok, stats = gate_pass(mults, list(range(len(mults))))
    assert not ok
    assert stats["drop3"] < 1                       # remove the top 3 and the "edge" is gone


def test_gate_fails_closed_on_tiny_samples():
    ok, stats = gate_pass([2.0], [0.0])
    assert not ok


def test_poisoned_client_writes_failed_row_and_never_raises(tmp_path):
    _, corpus = _build_universe()
    corpus_path = tmp_path / "corpus.json"
    corpus_path.write_text(json.dumps(corpus))
    st = LiveState(tmp_path / "s.db")
    v = run_remeasurement(st, corpus_path=corpus_path, cache_dir=tmp_path / "cache",
                          now=NOW, client=PoisonClient(), refresh_corpus=False, min_n=2)
    assert v["status"] == "failed"                  # returned, not raised
    run = st.latest_research_run()
    assert run is not None and run["status"] == "failed"
    # the terminal update landed on the SAME (single) row — never left 'running'
    rows = st.query("SELECT id, status, verdict_json FROM research_runs")
    assert len(rows) == 1 and rows[0]["status"] == "failed"
    vj = json.loads(rows[0]["verdict_json"])
    assert vj["error"] and vj["started_at"] and vj["finished_at"]
    assert any(a["kind"] == "RESEARCH_FAILED" for a in st.recent_alerts())
    assert st.get_system("last_research_at") is not None   # no weekly retry storm
    st.close()


class SpyClient(FakeClient):
    """FakeClient that snapshots the latest research_runs row from INSIDE the pricing loop,
    so the test can see the live 'running' progress mid-run."""

    def __init__(self, series_by_mint, state):
        super().__init__(series_by_mint)
        self.state = state
        self.snapshots: list[dict] = []

    def get_price_series(self, mint, start, end):
        row = self.state.latest_research_run()
        if row is not None:
            self.snapshots.append(row)
        return super().get_price_series(mint, start, end)


def test_running_row_shows_live_progress_and_terminal_lands_on_same_row(tmp_path):
    series, corpus = _build_universe(n=25)          # >20 tokens -> the every-20 tick fires
    corpus_path = tmp_path / "corpus.json"
    corpus_path.write_text(json.dumps(corpus))
    st = LiveState(tmp_path / "s.db")
    spy = SpyClient(series, st)
    run_remeasurement(st, corpus_path=corpus_path, cache_dir=tmp_path / "cache",
                      now=NOW, client=spy, refresh_corpus=False, min_n=2)
    # mid-run: a status='running' row existed, with pricing-phase progress fields
    assert spy.snapshots, "pricing loop never saw a research_runs row"
    assert all(s["status"] == "running" for s in spy.snapshots)
    verdicts = [json.loads(s["verdict_json"]) for s in spy.snapshots]
    pricing = [v for v in verdicts if v.get("phase") == "pricing"]
    assert pricing, f"no pricing-phase snapshot (saw {[v.get('phase') for v in verdicts]})"
    for v in pricing:
        assert v["started_at"] and v["total"] == 25 and v["n_skipped_unpriced"] == 0
        assert isinstance(v["priced"], int)
    assert max(v["priced"] for v in pricing) >= 20  # the every-~20-tokens update fired
    # terminal: the SAME row (same id), flipped to a terminal status, keeps started_at
    rows = st.query("SELECT id, status, verdict_json FROM research_runs")
    assert len(rows) == 1
    assert rows[0]["id"] == spy.snapshots[0]["id"]
    assert rows[0]["status"] == "stale_corpus"      # refresh_corpus=False, honestly labeled
    vj = json.loads(rows[0]["verdict_json"])
    assert vj["status"] == "stale_corpus" and vj["n_tokens"] == 25
    assert vj["started_at"] == verdicts[0]["started_at"] and vj["finished_at"]
    st.close()


def test_stale_running_rows_are_superseded_on_next_launch(tmp_path):
    series, corpus = _build_universe()
    corpus_path = tmp_path / "corpus.json"
    corpus_path.write_text(json.dumps(corpus))
    st = LiveState(tmp_path / "s.db")
    # a ghost from a hard-killed run: still 'running', never finalized
    ghost_id = st.record_research_run(
        status="running", verdict={"phase": "pricing", "started_at": "2026-06-01T00:00:00+00:00",
                                   "priced": 40, "total": 500, "n_skipped_unpriced": 0})
    run_remeasurement(st, corpus_path=corpus_path, cache_dir=tmp_path / "cache",
                      now=NOW, client=FakeClient(series), refresh_corpus=False, min_n=2)
    ghost = st.query("SELECT status, verdict_json FROM research_runs WHERE id=?", (ghost_id,))[0]
    assert ghost["status"] == "failed"
    assert json.loads(ghost["verdict_json"])["note"] == "superseded/stale"
    assert st.query("SELECT COUNT(*) AS n FROM research_runs WHERE status='running'")[0]["n"] == 0
    assert st.latest_research_run()["status"] == "stale_corpus"     # the new, real run
    st.close()


def test_lab_surfaces_running_progress(tmp_path):
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from dashboard.data import lab

    st = LiveState(tmp_path / "s.db")
    started_at = datetime.now(timezone.utc).isoformat()
    st.record_research_run(status="running",
                           verdict={"phase": "pricing", "started_at": started_at,
                                    "priced": 40, "total": 565, "n_skipped_unpriced": 2})
    out = lab(st)
    assert out["research_running"] is True
    lr = out["last_research"]
    assert lr["status"] == "running"                # row status merged into the verdict
    assert lr["phase"] == "pricing" and lr["priced"] == 40 and lr["total"] == 565
    assert lr["started_at"] == started_at and lr["n_skipped_unpriced"] == 2
    st.close()

    # a >2h-old 'running' ghost must NOT read as running (but stays visible as last_research)
    st2 = LiveState(tmp_path / "s2.db")
    old = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    st2.record_research_run(status="running",
                            verdict={"phase": "grid", "started_at": old,
                                     "priced": 565, "total": 565, "n_skipped_unpriced": 0})
    out2 = lab(st2)
    assert out2["research_running"] is False
    assert out2["last_research"]["status"] == "running"
    st2.close()
