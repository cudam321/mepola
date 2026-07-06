"""DexScreener client — free, no-API-key DEX market metadata.

Base URL: https://api.dexscreener.com  (free tier, ~60 requests/minute, no auth).

This module is used for *metadata* lookups only (ticker -> mint resolution, current
liquidity, token age). DexScreener does not serve OHLCV candles; use the GeckoTerminal
client for price series.

REAL RESPONSE SHAPES (observed June 2026 while probing live)
------------------------------------------------------------
GET /latest/dex/search?q={query}
    -> {"schemaVersion": "1.0.0", "pairs": [ <pair>, ... ]}   (max ~30 pairs)
       'pairs' is MIXED-CHAIN (e.g. q=SOL returns base/ethereum/solana pairs), so
       callers must filter on pair["chainId"] == "solana".
       For a query with no matches, "pairs" is [] (or occasionally null).

GET /token-pairs/v1/solana/{mint}
    -> BARE JSON LIST: [ <pair>, ... ]   (already chain-filtered to solana)
       For an unknown/invalid mint this returns [] with HTTP 200 (NOT a 404).

GET /latest/dex/tokens/{mint}   (alternative to token-pairs; same <pair> shape)
    -> {"schemaVersion": "1.0.0", "pairs": [ <pair>, ... ]}
       We prefer /token-pairs/v1/solana/{mint} because it returns a clean list and
       is pre-filtered to the solana chain.

<pair> object (fields actually observed; some are optional / may be absent):
    chainId        str    e.g. "solana"
    dexId          str    e.g. "raydium", "orca", "meteora", "pumpfun"
    url            str    dexscreener.com pair page
    pairAddress    str    on-chain pool/pair address
    labels         list   optional, e.g. ["v2"], ["DLMM"] (absent on some pairs)
    baseToken      {"address": <mint>, "name": str, "symbol": str}
                          NOTE: address is the TOKEN MINT we care about.
    quoteToken     {"address": str, "name": str, "symbol": str}  e.g. SOL / USDC
    priceNative    str    price in quote token (string-encoded float)
    priceUsd       str    price in USD (string-encoded float; may be absent)
    txns           {"m5"/"h1"/"h6"/"h24": {"buys": int, "sells": int}}
    volume         {"m5"/"h1"/"h6"/"h24": float}
    priceChange    {"m5"/"h1"/"h6"/"h24": float}  (percent)
    liquidity      {"usd": float, "base": float, "quote": float}
                          NOTE: liquidity (or liquidity.usd) can be missing/null on
                          thin or brand-new pairs.
    fdv            float  fully-diluted valuation (optional)
    marketCap      float  (optional)
    pairCreatedAt  int    POOL creation time in UNIX MILLISECONDS (optional; can be
                          absent on very new pools). The EARLIEST pairCreatedAt across
                          a mint's pools is used as the token's AGE proxy.
    info           {...}  image / websites / socials (optional)
"""

from __future__ import annotations

import json
import time
import urllib.parse
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

_BASE_URL = "https://api.dexscreener.com"
_USER_AGENT = "memebot/1.0 (+dexscreener-client)"


class DexScreenerClient:
    """Thin client over the free DexScreener REST API.

    Handles timeouts, a small retry/backoff, and 429 rate-limit sleeps. All methods
    are best-effort: on persistent failure they return an empty list / None rather
    than raising, so callers can degrade gracefully.
    """

    def __init__(
        self,
        *,
        base_url: str = _BASE_URL,
        timeout: float = 15.0,
        max_retries: int = 3,
        backoff_base: float = 0.5,
        client: Optional[httpx.Client] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self._owns_client = client is None
        self._client = client or httpx.Client(
            timeout=timeout,
            headers={"User-Agent": _USER_AGENT, "Accept": "application/json"},
            follow_redirects=True,
        )

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "DexScreenerClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    # Low-level HTTP with retry / backoff / 429 handling
    # ------------------------------------------------------------------ #
    def _get(self, path: str) -> Any:
        """GET {base_url}{path} and return decoded JSON, or None on failure."""
        url = f"{self.base_url}{path}"
        last_exc: Optional[Exception] = None
        for attempt in range(self.max_retries):
            try:
                resp = self._client.get(url)
            except httpx.HTTPError as exc:  # network / timeout
                last_exc = exc
                time.sleep(self.backoff_base * (2 ** attempt))
                continue

            if resp.status_code == 429:
                # Free-tier rate limit. Honor Retry-After if present, else back off.
                retry_after = resp.headers.get("Retry-After")
                try:
                    sleep_s = float(retry_after) if retry_after else self.backoff_base * (2 ** attempt)
                except ValueError:
                    sleep_s = self.backoff_base * (2 ** attempt)
                time.sleep(min(sleep_s, 30.0))
                continue

            if 500 <= resp.status_code < 600:
                time.sleep(self.backoff_base * (2 ** attempt))
                continue

            if resp.status_code != 200:
                # 4xx (other than 429) -> not retryable; treat as no data.
                return None

            try:
                return resp.json()
            except (json.JSONDecodeError, ValueError) as exc:
                last_exc = exc
                return None

        # Exhausted retries.
        _ = last_exc
        return None

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    def search(self, query: str) -> list[dict]:
        """Search across all chains; return the raw 'pairs' list (may be mixed-chain)."""
        q = urllib.parse.quote(query)
        data = self._get(f"/latest/dex/search?q={q}")
        if isinstance(data, dict):
            pairs = data.get("pairs")
            return pairs if isinstance(pairs, list) else []
        return []

    def token_pairs(self, mint: str) -> list[dict]:
        """All solana pairs for a mint. Returns [] for unknown mints (API gives [])."""
        mint_enc = urllib.parse.quote(mint)
        data = self._get(f"/token-pairs/v1/solana/{mint_enc}")
        if isinstance(data, list):
            return data
        # Defensive fallback: the /latest/dex/tokens endpoint wraps in {"pairs": [...]}.
        if isinstance(data, dict):
            pairs = data.get("pairs")
            return pairs if isinstance(pairs, list) else []
        return []

    def resolve_ticker_to_mint(
        self, ticker: str, *, prefer_network: str = "solana"
    ) -> Optional[str]:
        """Resolve a ticker/symbol to a mint address.

        Searches the ticker, keeps pairs on ``prefer_network`` whose baseToken symbol
        matches (case-insensitive, ignoring a leading '$'), and returns the
        baseToken.address of the highest-liquidity match. Returns None if nothing fits.
        """
        pairs = self.search(ticker)
        if not pairs:
            return None

        want = ticker.lstrip("$").strip().lower()
        best_addr: Optional[str] = None
        best_liq = -1.0
        for p in pairs:
            if p.get("chainId") != prefer_network:
                continue
            base = p.get("baseToken") or {}
            symbol = str(base.get("symbol") or "").lstrip("$").strip().lower()
            if symbol != want:
                continue
            addr = base.get("address")
            if not addr:
                continue
            liq = _liquidity_usd(p)
            if liq > best_liq:
                best_liq = liq
                best_addr = addr
        return best_addr

    def current_liquidity_usd(self, mint: str) -> Optional[float]:
        """Max liquidity.usd across the mint's solana pools. None if no data."""
        pairs = self.token_pairs(mint)
        if not pairs:
            return None
        liqs = [_liquidity_usd(p) for p in pairs]
        liqs = [v for v in liqs if v > 0.0]
        return max(liqs) if liqs else None

    def pair_created_at(self, mint: str) -> Optional[datetime]:
        """Earliest pairCreatedAt across the mint's pools -> tz-aware UTC datetime.

        This is the token's AGE proxy (oldest pool). None if no timestamp is available.
        """
        pairs = self.token_pairs(mint)
        earliest_ms: Optional[int] = None
        for p in pairs:
            ms = p.get("pairCreatedAt")
            if not isinstance(ms, (int, float)):
                continue
            ms = int(ms)
            if ms <= 0:
                continue
            if earliest_ms is None or ms < earliest_ms:
                earliest_ms = ms
        if earliest_ms is None:
            return None
        return datetime.fromtimestamp(earliest_ms / 1000.0, tz=timezone.utc)


# ---------------------------------------------------------------------- #
# Helpers
# ---------------------------------------------------------------------- #
def _liquidity_usd(pair: dict) -> float:
    """Extract liquidity.usd as a float; 0.0 when missing/null/unparseable."""
    liq = pair.get("liquidity")
    if not isinstance(liq, dict):
        return 0.0
    val = liq.get("usd")
    try:
        return float(val) if val is not None else 0.0
    except (TypeError, ValueError):
        return 0.0
