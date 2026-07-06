"""GeckoTerminal (CoinGecko on-chain) free API client.

Base URL: https://api.geckoterminal.com/api/v2  (no API key, ~30 req/min free tier).
Network slug used throughout: "solana".

Observed response shapes (probed live 2026-06):

GET /networks/{network}/tokens/{mint}/pools?include=base_token,quote_token,dex
    {
      "data": [
        {
          "id": "solana_<pool_addr>",
          "type": "pool",
          "attributes": {
            "address": "<pool_addr>",
            "name": "PUMP / SOL",                # "<base_sym> / <quote_sym>"
            "pool_created_at": "2025-07-14T16:55:54Z",
            "reserve_in_usd": "12632066.5702",     # <-- liquidity in USD (string)
            "base_token_price_usd": "...", ...
          },
          "relationships": {
            "base_token":  {"data": {"id": "solana_<mint>",  "type": "token"}},
            "quote_token": {"data": {"id": "solana_<mint>",  "type": "token"}},
            "dex":         {"data": {"id": "pumpswap",        "type": "dex"}}
          }
        }, ...
      ],
      "included": [                                # deduped; only present with ?include
        {"type": "token", "id": "solana_<mint>",
         "attributes": {"address": "...", "symbol": "USDC", "name": "...", ...}},
        {"type": "dex",   "id": "pumpswap", "attributes": {"name": "PumpSwap"}}, ...
      ]
    }
    Up to 20 pools, NOT guaranteed sorted by liquidity. `included` is deduped, so a
    pool's quote token may be missing from it -> fall back to the pool `name`.

    404 -> {"errors": [{"status": "404", "title": "Not Found"}], "meta": {...}}

GET /networks/{network}/pools/{pool}/ohlcv/{timeframe}?aggregate=1&limit=1000&currency=usd&token=base
    {
      "data": {
        "id": "...", "type": "ohlcv_request_response",
        "attributes": {
          "ohlcv_list": [
            [1782046800, 73.482..., 73.482..., 73.482..., 73.482..., 0.2646...],
            [1782046740, ...], ...                # [ts_seconds, o, h, l, c, v]
          ]                                        # NEWEST FIRST
        }
      },
      "meta": {"base": {...}, "quote": {...}}
    }
    `timeframe` path segment must be one of: day | hour | minute.
    `before_timestamp` (unix seconds) pages backwards: returns bars strictly older.

Rate limit (HTTP 429) body:
    {"status": {"error_code": 429, "error_message": "You've exceeded the Rate Limit..."}}
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx

from memebot.models import Candle, Pool, PriceSeries

_BASE_URL = "https://api.geckoterminal.com/api/v2"
_NETWORK = "solana"
_VALID_TIMEFRAMES = ("day", "hour", "minute")

# Free tier is ~30 req/min. The OHLCV endpoint caps `limit` at 1000 bars/request.
_MAX_OHLCV_LIMIT = 1000
# Use "minute" resolution when the requested window fits comfortably within the
# ~1000-bar single-request budget (1000 min ~= 16.6h); otherwise use "hour".
_MINUTE_WINDOW = timedelta(hours=16)


def _parse_iso8601(value: Optional[str]) -> Optional[datetime]:
    """Parse an ISO8601 timestamp (e.g. '2025-07-14T16:55:54Z') to tz-aware UTC."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _to_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class GeckoTerminalClient:
    """Thin, robust client for the GeckoTerminal v2 on-chain API."""

    def __init__(
        self,
        *,
        base_url: str = _BASE_URL,
        network: str = _NETWORK,
        timeout: float = 20.0,
        max_retries: int = 8,
        min_interval: float = 2.1,
        client: Optional[httpx.Client] = None,
    ) -> None:
        self._network = network
        self._max_retries = max_retries
        # Proactive throttle: free tier is ~30 req/min, so default to ~28/min to
        # avoid 429s entirely during multi-mint backtests.
        self._min_interval = min_interval
        self._last_req = 0.0
        self._owns_client = client is None
        self._client = client or httpx.Client(
            base_url=base_url,
            timeout=timeout,
            headers={"Accept": "application/json", "User-Agent": "memebot/1.0"},
        )

    # -- lifecycle --------------------------------------------------------- #
    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "GeckoTerminalClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- low-level HTTP ---------------------------------------------------- #
    def _get(self, path: str, params: Optional[dict[str, Any]] = None) -> Optional[dict[str, Any]]:
        """GET with retry/backoff. Returns parsed JSON, or None for a 404.

        Retries on 429 (rate limit) and 5xx with exponential backoff, honouring
        Retry-After when present. Returns None on a hard 404 (unknown mint/pool).
        """
        last_exc: Optional[Exception] = None
        for attempt in range(self._max_retries):
            # Proactive client-side rate limiting (keep under the free-tier cap).
            wait = self._min_interval - (time.monotonic() - self._last_req)
            if wait > 0:
                time.sleep(wait)
            self._last_req = time.monotonic()
            try:
                resp = self._client.get(path, params=params)
            except httpx.HTTPError as exc:  # network / timeout
                last_exc = exc
                time.sleep(min(2.0 ** attempt, 30.0))
                continue

            if resp.status_code == 404:
                return None
            if resp.status_code == 429 or resp.status_code >= 500:
                retry_after = resp.headers.get("Retry-After")
                if retry_after and retry_after.isdigit():
                    delay = float(retry_after)
                elif resp.status_code == 429:
                    # Free tier resets per-minute; wait out the window rather than give up.
                    delay = 20.0
                else:
                    delay = min(2.0 ** attempt, 30.0)
                time.sleep(min(delay, 60.0))
                continue

            resp.raise_for_status()
            return resp.json()

        if last_exc is not None:
            raise last_exc
        raise httpx.HTTPError("GeckoTerminal request exhausted retries (rate-limited)")

    # -- public API -------------------------------------------------------- #
    def find_pools(self, mint: str) -> list[Pool]:
        """Return pools whose base token is ``mint``, sorted by liquidity desc.

        ``base_mint`` is forced to the queried ``mint`` per the contract.
        """
        payload = self._get(
            f"/networks/{self._network}/tokens/{mint}/pools",
            params={"include": "base_token,quote_token,dex"},
        )
        if not payload or not payload.get("data"):
            return []

        included = self._index_included(payload.get("included", []))
        pools: list[Pool] = []
        for item in payload["data"]:
            attrs = item.get("attributes", {})
            rels = item.get("relationships", {})

            address = attrs.get("address") or _strip_network(item.get("id", ""))
            if not address:
                continue

            dex = _ref_id(rels.get("dex"))
            dex_name = included.get(("dex", dex), {}).get("name") if dex else None

            quote_id = _ref_id(rels.get("quote_token"), prefix=True)
            quote_symbol = None
            if quote_id is not None:
                quote_symbol = included.get(("token", quote_id), {}).get("symbol")
            if not quote_symbol:
                quote_symbol = _quote_symbol_from_name(attrs.get("name"))

            pools.append(
                Pool(
                    address=address,
                    dex=dex_name or dex or "",
                    base_mint=mint,
                    quote_symbol=quote_symbol or "",
                    network=self._network,
                    created_at=_parse_iso8601(attrs.get("pool_created_at")),
                    liquidity_usd=_to_float(attrs.get("reserve_in_usd")),
                )
            )

        pools.sort(key=lambda p: p.liquidity_usd if p.liquidity_usd is not None else -1.0, reverse=True)
        return pools

    def fetch_ohlcv(
        self,
        pool_address: str,
        timeframe: str,
        *,
        aggregate: int = 1,
        before_timestamp: Optional[int] = None,
        limit: int = 1000,
        currency: str = "usd",
        token: str = "base",
    ) -> list[Candle]:
        """Fetch OHLCV candles for one pool, returned ASCENDING by ts.

        ``timeframe`` must be one of day | hour | minute. The API returns newest
        first; this method converts unix-second timestamps to tz-aware UTC and
        reverses to ascending order.
        """
        if timeframe not in _VALID_TIMEFRAMES:
            raise ValueError(f"timeframe must be one of {_VALID_TIMEFRAMES}, got {timeframe!r}")

        params: dict[str, Any] = {
            "aggregate": aggregate,
            "limit": min(max(limit, 1), _MAX_OHLCV_LIMIT),
            "currency": currency,
            "token": token,
        }
        if before_timestamp is not None:
            params["before_timestamp"] = before_timestamp

        payload = self._get(
            f"/networks/{self._network}/pools/{pool_address}/ohlcv/{timeframe}",
            params=params,
        )
        if not payload:
            return []

        rows = payload.get("data", {}).get("attributes", {}).get("ohlcv_list", []) or []
        candles: list[Candle] = []
        for row in rows:
            if not row or len(row) < 6:
                continue
            ts_seconds = row[0]
            try:
                ts = datetime.fromtimestamp(int(ts_seconds), tz=timezone.utc)
            except (TypeError, ValueError, OverflowError, OSError):
                continue
            candles.append(
                Candle(
                    ts=ts,
                    open=float(row[1]),
                    high=float(row[2]),
                    low=float(row[3]),
                    close=float(row[4]),
                    volume=float(row[5]),
                )
            )

        candles.sort(key=lambda c: c.ts)  # API is newest-first -> ascending
        return candles

    def get_price_series(self, mint: str, start: datetime, end: datetime) -> PriceSeries:
        """Build a price series for ``mint`` covering [start, end].

        Picks the highest-liquidity pool, chooses minute vs hour resolution by
        window size, paginates backwards with ``before_timestamp`` to cover the
        whole range, and trims to roughly [start - 1 bar, end + 1 bar]. Returns
        an empty series (pool=None, candles=[]) when no pool or no data exists.
        """
        start = _as_utc(start)
        end = _as_utc(end)
        if end < start:
            start, end = end, start

        pools = self.find_pools(mint)
        if not pools:
            return PriceSeries(mint=mint, pool=None, timeframe="minute", aggregate=1, candles=[])
        pool = pools[0]

        timeframe = "minute" if (end - start) <= _MINUTE_WINDOW else "hour"
        bar = timedelta(minutes=1) if timeframe == "minute" else timedelta(hours=1)
        lo = start - bar
        hi = end + bar

        # Page backwards from just past `hi` until we reach `lo` or run dry.
        collected: dict[datetime, Candle] = {}
        before_ts: Optional[int] = int(hi.timestamp()) + 1
        # Safety bound on pagination requests (covers ~years of hourly data).
        for _ in range(40):
            batch = self.fetch_ohlcv(
                pool.address,
                timeframe,
                aggregate=1,
                before_timestamp=before_ts,
                limit=_MAX_OHLCV_LIMIT,
            )
            if not batch:
                break
            for candle in batch:
                collected[candle.ts] = candle
            oldest = batch[0].ts  # ascending -> first is oldest in this batch
            if oldest <= lo:
                break
            next_before = int(oldest.timestamp())
            if before_ts is not None and next_before >= before_ts:
                break  # no forward progress; avoid infinite loop
            before_ts = next_before

        candles = [c for ts, c in sorted(collected.items()) if lo <= c.ts <= hi]
        return PriceSeries(
            mint=mint,
            pool=pool,
            timeframe=timeframe,
            aggregate=1,
            candles=candles,
        )

    # -- helpers ----------------------------------------------------------- #
    @staticmethod
    def _index_included(included: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
        index: dict[tuple[str, str], dict[str, Any]] = {}
        for item in included:
            itype = item.get("type")
            iid = item.get("id")
            if itype and iid:
                index[(itype, iid)] = item.get("attributes", {})
        return index


def _ref_id(relationship: Optional[dict[str, Any]], *, prefix: bool = False) -> Optional[str]:
    """Pull the id out of a JSON:API relationship object.

    Token relationship ids are network-prefixed ('solana_<mint>') and match the
    `included` token ids, so keep the prefix for token lookups (prefix=True).
    Dex ids ('pumpswap') are unprefixed.
    """
    if not relationship:
        return None
    data = relationship.get("data")
    if not data:
        return None
    rid = data.get("id")
    if rid is None:
        return None
    return rid if prefix else _strip_network(rid)


def _strip_network(value: str) -> str:
    """'solana_<addr>' -> '<addr>'; leave already-bare ids untouched."""
    prefix = f"{_NETWORK}_"
    return value[len(prefix):] if value.startswith(prefix) else value


def _quote_symbol_from_name(name: Optional[str]) -> Optional[str]:
    """Pool name is '<base> / <quote>'; return the quote side."""
    if not name or "/" not in name:
        return None
    return name.split("/")[-1].strip() or None


def _as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)
