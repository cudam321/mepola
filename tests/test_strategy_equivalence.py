"""THE first correctness gate: the live state machine must reproduce the backtest.

Feeds each token's REAL cached candle series (the same series `stage38` consumes) through
`TailRider` and asserts the realized multiple equals the golden `sim` oracle to fp precision
for ANSEM (~197.6x) and a deterministic sample of first-call tokens.

Offline: uses the warm cache at `data_cache/jupiter_untrunc`. `NOW` is pinned to
2026-06-30 12:00 UTC so the fetch windows (and therefore the cache keys) match `stage14/38`
exactly. If the corpus or cache is absent the module is skipped.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest

from memebot.data.cache import CachedPriceClient
from memebot.data.jupiter import JupiterChartsClient
from memebot.ingest.telegram_mcp import first_call_per_mint, load_corpus_json
from memebot.live.strategy import TailRider
from memebot.models import PriceSeries

from sim_oracle import sim_multiple

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "runs" / "your_channel_fresh.json"
CACHE = ROOT / "data_cache" / "jupiter_untrunc"
NOW = datetime(2026, 6, 30, 12, 0, 0, tzinfo=timezone.utc)  # pinned, matches stage14/38
ANSEM = "9cRCn9rGT8V2imeM2BaKs13yhMEais3ruM3rPvTGpump"      # ANSEM-B, the ~197x runner

pytestmark = pytest.mark.skipif(
    not CORPUS.exists() or not CACHE.exists(),
    reason="requires runs/your_channel_fresh.json and the data_cache/jupiter_untrunc cache",
)


def _series_to_today(client, mint: str, t0: datetime) -> PriceSeries:
    """Verbatim reproduction of stage14.series_to_today (pinned NOW) so cache keys match."""
    end = min(t0 + timedelta(days=45), NOW)
    mn = client.get_price_series(mint, t0 - timedelta(minutes=5), t0 + timedelta(hours=12))
    rest_start = t0 + timedelta(hours=12)
    rest = (client.get_price_series(mint, rest_start, end)
            if end > rest_start else PriceSeries(mint, None, "hour", 1, []))
    boundary = mn.candles[-1].ts if mn.candles else t0
    candles = list(mn.candles) + [c for c in rest.candles if c.ts > boundary]
    candles.sort(key=lambda c: c.ts)
    return PriceSeries(mint=mint, pool=None, timeframe="mixed", aggregate=1, candles=candles)


def _cds(client, signal):
    """The candle window stage38 uses: candles at/after posted_at, sig = first open."""
    try:
        ser = _series_to_today(client, signal.mint, signal.posted_at)
    except Exception:
        return None
    if not ser or not ser.candles:
        return None
    cds = [c for c in ser.candles if c.ts >= signal.posted_at]
    if not cds or cds[0].open <= 0:
        return None
    return cds


def _compare(cds):
    """Return (oracle_multiple, live_multiple) for one token's candle window."""
    H = np.array([c.high for c in cds]); L = np.array([c.low for c in cds])
    C = np.array([c.close for c in cds]); T = np.array([c.ts.timestamp() for c in cds])
    oracle = sim_multiple(H, L, C, T, cds[0].open)

    tr = TailRider()
    for c in cds:
        tr.on_candle(c)
    tr.finalize(cds[-1].close, cds[-1].ts)
    return oracle, tr.realized_multiple


@pytest.fixture(scope="module")
def client():
    return CachedPriceClient(JupiterChartsClient(min_interval=0.4), str(CACHE))


@pytest.fixture(scope="module")
def calls():
    cs = [s for s in first_call_per_mint(load_corpus_json(str(CORPUS))) if s.mint]
    return sorted(cs, key=lambda s: s.posted_at)


def test_ansem_reproduces_backtest(client, calls):
    s = next((s for s in calls if s.mint == ANSEM), None)
    assert s is not None, "ANSEM must be a first-call in the corpus"
    cds = _cds(client, s)
    assert cds is not None, "ANSEM candle series must be present in the cache"
    oracle, live = _compare(cds)
    assert oracle is not None and live is not None
    # exact equivalence to the backtest...
    assert live == pytest.approx(oracle, rel=1e-9, abs=1e-9)
    # ...and it is the ~197.6x runner config #1 rides
    assert live == pytest.approx(197.6, abs=1.0)


def test_sampled_tokens_reproduce_backtest(client, calls):
    # A deterministic, evenly-spaced sample of first-calls (+ ANSEM), each pinned to the oracle.
    step = max(1, len(calls) // 24)
    sample = calls[::step][:24]
    if all(s.mint != ANSEM for s in sample):
        ansem = next((s for s in calls if s.mint == ANSEM), None)
        if ansem:
            sample.append(ansem)

    compared = 0
    mismatches = []
    for s in sample:
        cds = _cds(client, s)
        if cds is None:
            continue
        oracle, live = _compare(cds)
        if oracle is None:
            # the dip never triggered -> the machine must also decline to enter
            assert live is None, f"{s.mint}: oracle=None but live entered ({live})"
            compared += 1
            continue
        if live != pytest.approx(oracle, rel=1e-9, abs=1e-9):
            mismatches.append((s.mint, oracle, live))
        compared += 1

    assert compared >= 15, f"expected to compare >=15 tokens, only {compared} had cached data"
    assert not mismatches, f"live machine diverged from the backtest on: {mismatches}"
