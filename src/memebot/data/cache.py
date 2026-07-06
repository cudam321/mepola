"""On-disk cache wrapper for any price client with ``get_price_series``.

Backtesting 1000+ tokens against a 30 req/min free API is slow and fragile; caching makes
runs incremental and resumable (interrupt and re-run — successful fetches are reused, only
missing ones hit the network). Keyed by (mint, start, end) so a given token+window is
fetched once. Change the horizons (window) -> new key -> refetch.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from memebot.models import Candle, Pool, PriceSeries


class _Inner(Protocol):
    def get_price_series(self, mint: str, start: datetime, end: datetime) -> PriceSeries: ...


def _dump(s: PriceSeries) -> str:
    pool = None
    if s.pool is not None:
        pool = {
            "address": s.pool.address, "dex": s.pool.dex, "base_mint": s.pool.base_mint,
            "quote_symbol": s.pool.quote_symbol, "network": s.pool.network,
            "created_at": s.pool.created_at.isoformat() if s.pool.created_at else None,
            "liquidity_usd": s.pool.liquidity_usd,
        }
    return json.dumps({
        "mint": s.mint, "pool": pool, "timeframe": s.timeframe, "aggregate": s.aggregate,
        "candles": [[int(c.ts.timestamp()), c.open, c.high, c.low, c.close, c.volume] for c in s.candles],
    })


def _load(text: str) -> PriceSeries:
    d = json.loads(text)
    pool = None
    if d.get("pool"):
        p = d["pool"]
        ca = p.get("created_at")
        pool = Pool(address=p["address"], dex=p["dex"], base_mint=p["base_mint"],
                    quote_symbol=p["quote_symbol"], network=p.get("network", "solana"),
                    created_at=datetime.fromisoformat(ca) if ca else None,
                    liquidity_usd=p.get("liquidity_usd"))
    candles = [Candle(ts=datetime.fromtimestamp(r[0], tz=timezone.utc),
                      open=r[1], high=r[2], low=r[3], close=r[4], volume=r[5])
               for r in d.get("candles", [])]
    return PriceSeries(mint=d["mint"], pool=pool, timeframe=d["timeframe"],
                       aggregate=d["aggregate"], candles=candles)


class CachedPriceClient:
    def __init__(self, inner: _Inner, cache_dir: str | Path) -> None:
        self.inner = inner
        self.dir = Path(cache_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.hits = 0
        self.misses = 0

    def get_price_series(self, mint: str, start: datetime, end: datetime) -> PriceSeries:
        key = f"{mint}_{int(start.timestamp())}_{int(end.timestamp())}.json"
        path = self.dir / key
        if path.exists():
            try:
                series = _load(path.read_text())
                self.hits += 1
                return series
            except Exception:
                pass  # corrupt cache entry -> refetch
        series = self.inner.get_price_series(mint, start, end)
        self.misses += 1
        try:
            path.write_text(_dump(series))
        except Exception:
            pass
        return series
