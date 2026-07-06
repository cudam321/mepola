"""Jupiter API client (free keyless ``lite-api.jup.ag``, ~1 req/s).

Roles in this system:

  * ``JupiterChartsClient`` (datapi, bottom of file) — token-level HISTORICAL OHLCV by
    mint, keyless, ONE call per token, recent and old/dead tokens alike. This is the
    primary Phase 0.5 backtest price source (faster than per-pool GeckoTerminal).
  * ``resolve_symbol_to_mint`` — RELIABLE ticker->mint resolution. ``tokens/v2/search``
    returns canonical, verified tokens (BONK -> DezXAZ8z..., not a copycat), fixing the
    mis-resolution DexScreener gave us.
  * ``shield`` — token safety warnings per mint (cross-check for the RugCheck hard gate).
  * ``price`` / ``price_full`` — live USD price + 24h change (Phase 2 paper-on-live-stream).

Verified live shapes (lite-api.jup.ag, 2026-06):
  GET /price/v3?ids=<m1,m2>      -> { "<mint>": {"usdPrice", "liquidity", "decimals",
                                       "priceChange24h", "blockId", "createdAt"}, ... }
  GET /tokens/v2/search?query=Q  -> [ {"id"(=mint), "name", "symbol", "decimals",
                                       "usdPrice", "liquidity", "holderCount", "mcap",
                                       "fdv", "organicScore", ...}, ... ]
  GET /ultra/v1/shield?mints=<m1,m2> -> { "warnings": { "<mint>": [ {...}, ... ] } }

With an API key, set ``api_key`` and the base flips to ``api.jup.ag`` with an
``x-api-key`` header (higher rate limits); paths are identical.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx

from memebot.models import Candle, PriceSeries

_LITE_BASE = "https://lite-api.jup.ag"
_PRO_BASE = "https://api.jup.ag"
_DATAPI_BASE = "https://datapi.jup.ag"


class JupiterClient:
    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout: float = 15.0,
        max_retries: int = 4,
        min_interval: float = 1.05,   # keyless free tier ~1 req/s
        client: Optional[httpx.Client] = None,
    ) -> None:
        self._api_key = api_key or None
        base = base_url or (_PRO_BASE if self._api_key else _LITE_BASE)
        self._max_retries = max_retries
        self._min_interval = min_interval
        self._last_req = 0.0
        self._rate_lock = threading.Lock()   # thread-safe rate gate (concurrent executor workers)
        headers = {"Accept": "application/json", "User-Agent": "memebot/1.0"}
        if self._api_key:
            headers["x-api-key"] = self._api_key
        self._owns_client = client is None
        self._client = client or httpx.Client(base_url=base, timeout=timeout, headers=headers)

    # -- lifecycle --------------------------------------------------------- #
    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "JupiterClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- low-level HTTP ---------------------------------------------------- #
    def _get(self, path: str, params: dict[str, Any]) -> Optional[Any]:
        last_exc: Optional[Exception] = None
        for attempt in range(self._max_retries):
            # rate gate under a lock so concurrent workers space requests correctly; the HTTP
            # call itself is OUTSIDE the lock so requests overlap (only the spacing serializes).
            with self._rate_lock:
                wait = self._min_interval - (time.monotonic() - self._last_req)
                if wait > 0:
                    time.sleep(wait)
                self._last_req = time.monotonic()
            try:
                resp = self._client.get(path, params=params)
            except httpx.HTTPError as exc:
                last_exc = exc
                time.sleep(min(2.0 ** attempt, 30.0))
                continue
            if resp.status_code == 404:
                return None
            if resp.status_code == 429 or resp.status_code >= 500:
                retry_after = resp.headers.get("Retry-After")
                delay = float(retry_after) if (retry_after and retry_after.isdigit()) else min(2.0 ** attempt, 30.0)
                time.sleep(max(delay, self._min_interval))
                continue
            resp.raise_for_status()
            return resp.json()
        if last_exc is not None:
            raise last_exc
        raise httpx.HTTPError("Jupiter request exhausted retries (rate-limited)")

    # -- public API -------------------------------------------------------- #
    def price_full(self, mints: list[str]) -> dict[str, dict[str, Any]]:
        """Full price objects keyed by mint (up to 50 mints/request)."""
        if not mints:
            return {}
        out: dict[str, dict[str, Any]] = {}
        for i in range(0, len(mints), 50):
            chunk = mints[i:i + 50]
            data = self._get("/price/v3", {"ids": ",".join(chunk)})
            if isinstance(data, dict):
                out.update(data)
        return out

    def price(self, mints: list[str]) -> dict[str, float]:
        """USD price keyed by mint (missing/illiquid mints omitted)."""
        out: dict[str, float] = {}
        for mint, obj in self.price_full(mints).items():
            px = obj.get("usdPrice")
            if isinstance(px, (int, float)):
                out[mint] = float(px)
        return out

    def search(self, query: str, *, limit: int = 20) -> list[dict[str, Any]]:
        data = self._get("/tokens/v2/search", {"query": query})
        rows = data if isinstance(data, list) else (data or {}).get("tokens", [])
        return rows[:limit]

    def resolve_symbol_to_mint(self, symbol: str) -> Optional[str]:
        """Resolve a ticker to its CANONICAL mint via Jupiter token search.

        Filters to exact symbol matches (case-insensitive, leading '$' stripped) and
        returns the most liquid one. Far more reliable than DEX-pair ticker search,
        which returns copycats.
        """
        want = symbol.strip().lstrip("$").lower()
        if not want:
            return None
        matches = [t for t in self.search(symbol) if str(t.get("symbol", "")).lower() == want]
        if not matches:
            return None
        matches.sort(key=lambda t: (t.get("liquidity") or 0.0, t.get("holderCount") or 0), reverse=True)
        return matches[0].get("id")

    def shield(self, mints: list[str]) -> dict[str, list[dict[str, Any]]]:
        """Token safety warnings keyed by mint (empty list == no warnings)."""
        if not mints:
            return {}
        out: dict[str, list[dict[str, Any]]] = {}
        for i in range(0, len(mints), 50):
            chunk = mints[i:i + 50]
            data = self._get("/ultra/v1/shield", {"mints": ",".join(chunk)})
            warnings = (data or {}).get("warnings", {}) if isinstance(data, dict) else {}
            for mint, w in warnings.items():
                out[mint] = w if isinstance(w, list) else []
        return out


# --------------------------------------------------------------------------- #
# Jupiter datapi charts — token-level historical OHLCV (keyless, 1 call/token)
# --------------------------------------------------------------------------- #
# Verified live (2026-06): GET https://datapi.jup.ag/v2/charts/{mint}
#   ?interval=1_MINUTE|5_MINUTE|15_MINUTE|1_HOUR|1_DAY &from=<ISO8601Z> &to=<ISO8601Z>
#   &candles=<int, REQUIRED max count>
#   -> {"candles":[{"time":<unix s>,"open","high","low","close","volume"}, ...]}
# Works for both fresh and 5-month-old/dead memecoins by mint. No find-pool needed.
_DATAPI_INTERVAL = {"minute": "1_MINUTE", "hour": "1_HOUR", "day": "1_DAY"}
_MINUTE_SPAN = timedelta(hours=16)
_HOUR_SPAN = timedelta(days=40)


def _iso_z(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class JupiterChartsClient:
    """Historical OHLCV by token mint via Jupiter's datapi. Drop-in price source for
    the backtest (implements get_price_series). Faster than per-pool sources: one
    keyless call per token, recent and old tokens alike."""

    def __init__(
        self,
        *,
        base_url: str = _DATAPI_BASE,
        timeout: float = 20.0,
        max_retries: int = 5,
        min_interval: float = 0.4,
        client: Optional[httpx.Client] = None,
    ) -> None:
        self._max_retries = max_retries
        self._min_interval = min_interval
        self._last_req = 0.0
        self._rate_lock = threading.Lock()   # thread-safe rate gate
        self._owns_client = client is None
        self._client = client or httpx.Client(
            base_url=base_url, timeout=timeout,
            headers={"Accept": "application/json", "User-Agent": "memebot/1.0"},
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "JupiterChartsClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _get(self, path: str, params: dict[str, Any]) -> Optional[dict[str, Any]]:
        last_exc: Optional[Exception] = None
        for attempt in range(self._max_retries):
            with self._rate_lock:            # spacing serialized; the HTTP call overlaps
                wait = self._min_interval - (time.monotonic() - self._last_req)
                if wait > 0:
                    time.sleep(wait)
                self._last_req = time.monotonic()
            try:
                resp = self._client.get(path, params=params)
            except httpx.HTTPError as exc:
                last_exc = exc
                time.sleep(min(2.0 ** attempt, 20.0))
                continue
            if resp.status_code in (400, 404):
                return None  # unknown/uncharted mint or bad range
            if resp.status_code == 429 or resp.status_code >= 500:
                retry_after = resp.headers.get("Retry-After")
                delay = float(retry_after) if (retry_after and retry_after.isdigit()) else min(2.0 ** attempt + 1, 20.0)
                time.sleep(delay)
                continue
            resp.raise_for_status()
            return resp.json()
        if last_exc is not None:
            raise last_exc
        raise httpx.HTTPError("Jupiter datapi exhausted retries")

    def fetch_candles(self, mint: str, interval: str, start: datetime, end: datetime,
                      *, candles: int = 1000) -> list[Candle]:
        data = self._get(f"/v2/charts/{mint}", {
            "interval": interval, "from": _iso_z(start), "to": _iso_z(end), "candles": candles,
        })
        rows = (data or {}).get("candles", []) if isinstance(data, dict) else []
        out: list[Candle] = []
        for r in rows:
            try:
                out.append(Candle(
                    ts=datetime.fromtimestamp(int(r["time"]), tz=timezone.utc),
                    open=float(r["open"]), high=float(r["high"]), low=float(r["low"]),
                    close=float(r["close"]), volume=float(r.get("volume", 0.0)),
                ))
            except (KeyError, TypeError, ValueError):
                continue
        out.sort(key=lambda c: c.ts)
        return out

    def get_price_series(self, mint: str, start: datetime, end: datetime) -> PriceSeries:
        if end < start:
            start, end = end, start
        span = end - start
        if span <= _MINUTE_SPAN:
            tf = "minute"
        elif span <= _HOUR_SPAN:
            tf = "hour"
        else:
            tf = "day"
        bar = {"minute": timedelta(minutes=1), "hour": timedelta(hours=1), "day": timedelta(days=1)}[tf]
        lo, hi = start - bar, end + bar
        candles = self.fetch_candles(mint, _DATAPI_INTERVAL[tf], lo, hi, candles=1000)
        candles = [c for c in candles if lo <= c.ts <= hi]
        return PriceSeries(mint=mint, pool=None, timeframe=tf, aggregate=1, candles=candles)
