"""Dune Analytics API client — inline custom SQL (the on-chain pivot's universe source).

Verified working 2026-06-23 on Free-tier keys via ``POST /api/v1/sql/execute`` (despite the
dune-client SDK docstring claiming that path is Plus-only). Round trip: execute inline SQL ->
poll ``/execution/{id}/status`` -> fetch ``/execution/{id}/results``. Tracks ``datapoint_count``
so runs stay inside the ~2,500 credits/mo free budget (we hold two keys = ~5,000/mo headroom).

Note: API-triggered executions DO spend credits on Free (only the web editor's executions are
free). Read results once and cache to disk; do not re-execute the same query.
"""

from __future__ import annotations

import os
import time
from typing import Any, Optional

import httpx

_BASE = "https://api.dune.com/api/v1"
_TERMINAL_BAD = {"QUERY_STATE_FAILED", "QUERY_STATE_CANCELLED", "QUERY_STATE_EXPIRED"}


class DuneError(RuntimeError):
    pass


class DuneClient:
    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        performance: str = "medium",
        timeout: float = 30.0,
        poll_interval: float = 1.0,
        max_poll: int = 900,
        min_interval: float = 1.6,   # Free tier = 40 req/min -> >= 1.5s between requests
    ) -> None:
        self._key = api_key or os.environ.get("DUNE_API_KEY", "")
        if not self._key:
            raise DuneError("no Dune API key (set DUNE_API_KEY or pass api_key=)")
        self._perf = performance
        self._poll_interval = poll_interval
        self._max_poll = max_poll
        self._min_interval = min_interval
        self._last = 0.0
        self._client = httpx.Client(
            base_url=_BASE, timeout=timeout,
            headers={"X-DUNE-API-KEY": self._key, "Content-Type": "application/json"},
        )
        self.datapoints = 0  # cumulative this session, for credit budgeting

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "DuneClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _throttle(self) -> None:
        wait = self._min_interval - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()

    def _req(self, method: str, path: str, **kw: Any) -> dict[str, Any]:
        last_exc: Optional[Exception] = None
        for attempt in range(6):
            self._throttle()
            try:
                r = self._client.request(method, path, **kw)
            except httpx.HTTPError as exc:
                last_exc = exc
                time.sleep(min(2.0 ** attempt, 30.0))
                continue
            if r.status_code == 429 or r.status_code >= 500:
                ra = r.headers.get("Retry-After")
                time.sleep(float(ra) if (ra and ra.isdigit()) else min(2.0 ** attempt, 30.0))
                continue
            if r.status_code >= 400:
                raise DuneError(f"{method} {path} -> {r.status_code}: {r.text[:400]}")
            return r.json()
        if last_exc is not None:
            raise last_exc
        raise DuneError(f"{method} {path} exhausted retries")

    def usage(self) -> dict[str, Any]:
        """Free metadata endpoint: current billing period credits_used / credits_included."""
        res = self._req("POST", "/usage", json={})
        bp = (res.get("billing_periods") or [{}])[0]
        return {
            "credits_used": float(bp.get("credits_used", 0.0)),
            "credits_included": float(bp.get("credits_included", 0.0)),
        }

    def run_sql(self, sql: str, *, performance: Optional[str] = None) -> dict[str, Any]:
        """Execute inline SQL, poll to completion, return rows + columns + datapoints spent."""
        eid = self._req("POST", "/sql/execute",
                        json={"sql": sql, "performance": performance or self._perf})["execution_id"]
        state = None
        for _ in range(self._max_poll):
            st = self._req("GET", f"/execution/{eid}/status")
            state = st.get("state")
            if state == "QUERY_STATE_COMPLETED":
                break
            if state in _TERMINAL_BAD:
                raise DuneError(f"execution {eid} {state}: {st}")
            time.sleep(self._poll_interval)
        if state != "QUERY_STATE_COMPLETED":
            raise DuneError(f"execution {eid} unfinished after {self._max_poll} polls")
        res = self._req("GET", f"/execution/{eid}/results")
        result = res.get("result", {})
        meta = result.get("metadata", {})
        dp = int(meta.get("datapoint_count", 0))
        self.datapoints += dp
        return {
            "rows": result.get("rows", []),
            "columns": meta.get("column_names", []),
            "datapoints": dp,
            "row_count": int(meta.get("total_row_count", len(result.get("rows", [])))),
            "execution_id": eid,
        }
