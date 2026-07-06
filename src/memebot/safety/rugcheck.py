"""RugCheck.xyz free read API client (no API key required).

Base URL: https://api.rugcheck.xyz

Observed response shapes (probed live 2026-06):

GET /v1/tokens/{mint}/report/summary           # flat, lightweight
    {
      "tokenProgram": "Tokenkeg...",
      "tokenType": "",
      "risks": [ {"name": "...", "value": "...", "description": "...",
                  "score": 100, "level": "warn"}, ... ],   # often []
      "score": 1,
      "score_normalised": 1,                    # 0..100, HIGHER = riskier
      "lpLockedPct": 49.89...                    # single aggregate LP-locked %
    }

GET /v1/tokens/{mint}/report                    # full report (used here)
    {
      "mint": "<mint>",
      "mintAuthority":   null | {"owner": "...", "lamports": ..., "data": [...], ...},
      "freezeAuthority": null | {...},           # NON-null object == authority STILL set
      "score": 1,
      "score_normalised": 7,                     # 0..100, HIGHER = riskier
      "rugged": false,
      "risks": [ {"name": "Mutable metadata", "value": "",
                  "description": "...", "score": 100, "level": "warn"}, ... ],
      "markets": [
        {
          "pubkey": "...", "marketType": "pump_amm",
          "mintA": "...", "mintB": "...",
          "lp": {
            "baseMint": "...", "quoteMint": "So111...112",
            "lpLocked": 4193388308928, "lpUnlocked": 0,
            "lpLockedPct": 100,                  # <-- per-market LP locked/burned %
            "lpLockedUSD": 46571.89, ...
          }
        }, ...
      ],
      "topHolders": [
        {"address": "...", "owner": "...", "amount": 2069..., "decimals": 6,
         "pct": 20.690..., "insider": false, "uiAmountString": "..."}, ...
      ],                                          # up to 20, descending by pct
      "totalHolders": ..., "totalMarketLiquidity": ..., "token": {...},
      "tokenMeta": {...}, "fileMeta": {...}, ...
    }

Key field semantics:
  * mintAuthority / freezeAuthority: ``null`` => authority REVOKED (good).
    A non-null object means the authority is still held (not revoked).
  * score_normalised is 0..100 and HIGHER = riskier (SOL=1, BONK=7).
  * LP locked/burned %: taken from the markets[].lp.lpLockedPct of the deepest
    market (max lpLockedUSD). The summary endpoint's flat ``lpLockedPct`` is a
    convenient fallback but we use the full report so we also get authorities and
    holders in one call.
  * top10_holder_pct: sum of the first 10 topHolders[].pct.

Error / unknown-token behaviour (note: NOT 404):
    Unknown but well-formed mint  -> HTTP 400  {"error": "not found"}
    Malformed mint (bad base58 /
    wrong length)                 -> HTTP 400  {"error": "invalid length, ..."}
                                                {"error": "decode: invalid base58 ..."}
    These are treated as "no RugCheck data" and yield an all-None SafetyReport
    carrying a ``no_rugcheck_data`` note in ``risks``.

Rate limiting: the free tier may return HTTP 429; we honour Retry-After and back
off before retrying. 5xx are retried with exponential backoff.
"""

from __future__ import annotations

import time
from typing import Any, Optional

import httpx

from memebot.models import SafetyReport

_BASE_URL = "https://api.rugcheck.xyz"

# RugCheck signals an unknown/un-indexed token with HTTP 400 + this error string
# (rather than a 404). Treat it as "no data" instead of a hard failure.
_NOT_FOUND_ERRORS = ("not found", "invalid length", "decode")


class RugCheckClient:
    """Thin, robust client for the RugCheck.xyz free read API."""

    def __init__(
        self,
        *,
        base_url: str = _BASE_URL,
        timeout: float = 20.0,
        max_retries: int = 4,
        client: Optional[httpx.Client] = None,
    ) -> None:
        self._max_retries = max_retries
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

    def __enter__(self) -> "RugCheckClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- low-level HTTP ---------------------------------------------------- #
    def _get(self, path: str) -> Optional[dict[str, Any]]:
        """GET with retry/backoff.

        Returns parsed JSON on success, or ``None`` when RugCheck has no data for
        the token (its "not found" / malformed-mint 400 responses). Retries on 429
        (honouring Retry-After) and 5xx with exponential backoff. Raises on other
        non-2xx responses or exhausted retries.
        """
        last_exc: Optional[Exception] = None
        for attempt in range(self._max_retries):
            try:
                resp = self._client.get(path)
            except httpx.HTTPError as exc:  # network / timeout
                last_exc = exc
                time.sleep(min(2.0 ** attempt, 30.0))
                continue

            status = resp.status_code
            if status == 404 or (status == 400 and _is_not_found(resp)):
                return None
            if status == 429 or status >= 500:
                retry_after = resp.headers.get("Retry-After")
                if retry_after and retry_after.isdigit():
                    delay = float(retry_after)
                else:
                    delay = 5.0 * (attempt + 1) if status == 429 else min(2.0 ** attempt, 30.0)
                time.sleep(delay)
                continue

            resp.raise_for_status()
            return resp.json()

        if last_exc is not None:
            raise last_exc
        raise httpx.HTTPError("RugCheck request exhausted retries (rate-limited)")

    # -- public API -------------------------------------------------------- #
    def get_report(self, mint: str) -> SafetyReport:
        """Fetch the full RugCheck report for ``mint`` and map it to a SafetyReport.

        On an unknown token (no RugCheck data) returns a SafetyReport with all
        primitive fields None and a ``no_rugcheck_data`` note in ``risks``.
        """
        payload = self._get(f"/v1/tokens/{mint}/report")
        if not payload:
            return SafetyReport(
                mint=mint,
                risks=["no_rugcheck_data"],
                raw={},
            )

        return SafetyReport(
            mint=mint,
            mint_authority_revoked=_authority_revoked(payload.get("mintAuthority")),
            freeze_authority_revoked=_authority_revoked(payload.get("freezeAuthority")),
            lp_locked_or_burned_pct=_lp_locked_pct(payload.get("markets")),
            top10_holder_pct=_top10_pct(payload.get("topHolders")),
            risk_score=_to_float(payload.get("score_normalised")),
            risks=_risk_names(payload.get("risks")),
            raw=payload,
        )


# --------------------------------------------------------------------------- #
# Mapping helpers
# --------------------------------------------------------------------------- #
def _authority_revoked(authority: Any) -> Optional[bool]:
    """``null`` authority => revoked (True). A non-null object => still set (False).

    Returns None only if the key was entirely absent (None vs. missing is
    indistinguishable here, so absence also reads as revoked; RugCheck always
    emits the key, so this is effectively never None in practice)."""
    return authority is None


def _lp_locked_pct(markets: Any) -> Optional[float]:
    """LP locked/burned % from the deepest market (max lpLockedUSD).

    Falls back to any market that reports an ``lpLockedPct`` if USD sizing is
    missing. Returns None when there are no markets / no LP info.
    """
    if not isinstance(markets, list) or not markets:
        return None

    best_pct: Optional[float] = None
    best_usd = -1.0
    for market in markets:
        if not isinstance(market, dict):
            continue
        lp = market.get("lp")
        if not isinstance(lp, dict):
            continue
        pct = _to_float(lp.get("lpLockedPct"))
        if pct is None:
            continue
        usd = _to_float(lp.get("lpLockedUSD"))
        weight = usd if usd is not None else 0.0
        if weight > best_usd:
            best_usd = weight
            best_pct = pct
    return best_pct


def _top10_pct(top_holders: Any) -> Optional[float]:
    """Sum of the top-10 holders' ``pct`` values. None if no holder data."""
    if not isinstance(top_holders, list) or not top_holders:
        return None
    total = 0.0
    seen = False
    for holder in top_holders[:10]:
        if not isinstance(holder, dict):
            continue
        pct = _to_float(holder.get("pct"))
        if pct is not None:
            total += pct
            seen = True
    return total if seen else None


def _risk_names(risks: Any) -> list[str]:
    """Extract ``risks[].name`` strings, skipping malformed entries."""
    if not isinstance(risks, list):
        return []
    names: list[str] = []
    for risk in risks:
        if isinstance(risk, dict):
            name = risk.get("name")
            if name:
                names.append(str(name))
    return names


def _is_not_found(resp: httpx.Response) -> bool:
    """True if a 400 body is RugCheck's 'unknown / malformed token' error."""
    try:
        error = resp.json().get("error", "")
    except (ValueError, AttributeError):
        return False
    if not isinstance(error, str):
        return False
    error = error.lower()
    return any(marker in error for marker in _NOT_FOUND_ERRORS)


def _to_float(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
