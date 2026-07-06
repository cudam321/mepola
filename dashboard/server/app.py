"""FastAPI backend for the tail-rider dashboard.

- GET  /api/snapshot  -> the full dashboard payload (read-only from runs/live_state.db)
- GET  /api/control   -> runtime control knobs (editable ctl_* + kill_switch) and the LOCKED strategy
- POST /api/control   -> set ONE allowlisted knob (validated, bounded); strategy params are rejected
- GET  /api/token/{mint}/candles -> OHLCV for the token view (KEYLESS datapi, TTL-cached, clamped)
- GET  /api/token/{mint}/live    -> spot price/liquidity (KEYLESS lite-api, TTL-cached)
- WS   /ws            -> pushes a fresh snapshot whenever the DB changes, plus a heartbeat
- /                   -> serves the built React frontend (dashboard/frontend/dist) if present

Reads open the DB read-only; the ONLY write path is POST /api/control, which briefly opens the DB
writable to set a single allowlisted system_state key. Run:

    uv run --extra dashboard uvicorn dashboard.server.app:app --reload --port 8000
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from dashboard import data
from dashboard.server.auth import BasicAuthMiddleware
from memebot.config import Settings
from memebot.data.jupiter import JupiterChartsClient, JupiterClient
from memebot.live.risk import STAKE_HARD_CAP_USD

DB_PATH = os.environ.get("MEMEBOT_DB", str(data.DEFAULT_DB))
FRONTEND_DIST = Path(__file__).resolve().parents[1] / "frontend" / "dist"

app = FastAPI(title="memebot tail-rider dashboard")
app.add_middleware(
    # Served same-origin behind the dashboard's own host; the only cross-origin need is none.
    # allow_credentials stays False, so a wildcard origin cannot be abused to read authed responses.
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)
# Basic auth for the public deploy. Wire password=None ALWAYS (audit #32) so the middleware re-reads
# os.environ["DASHBOARD_PASSWORD"] per request: prod (Railway keeps it set) is unchanged, and the test
# suite stays hermetic (a stale DASHBOARD_PASSWORD in a sourced .env can no longer be captured at import
# and 401 every API test). start.sh refuses to boot the public container when it is empty (audit #4).
# /api/health stays exempt for Railway healthchecks.
app.add_middleware(BasicAuthMiddleware, password=None)


def _paper_db() -> Optional[str]:
    """Path to the paper twin (measurement book) DB, or None if it doesn't exist yet.
    Env read per-request (hermetic for tests; start.sh exports MEMEBOT_PAPER_DB on Railway)."""
    p = os.environ.get("MEMEBOT_PAPER_DB", str(data.DEFAULT_PAPER_DB))
    return p if Path(p).exists() else None


def _db_for(book: str) -> Optional[str]:
    """Resolve a `book` query param ('live' | 'paper') to a DB path. None = paper book absent."""
    return _paper_db() if book == "paper" else DB_PATH


def _book_err(book: str):
    """422 for a bad book value, 404 when the paper book doesn't exist yet, else None."""
    if book not in ("live", "paper"):
        return JSONResponse({"error": 'book must be "live" or "paper"'}, status_code=422)
    if book == "paper" and _paper_db() is None:
        return JSONResponse({"error": "no paper book yet (paper twin not started)"}, status_code=404)
    return None


def _snapshot(db: Optional[str] = None) -> dict:
    st = data.open_state(db or DB_PATH)
    try:
        return data.snapshot(st)
    finally:
        st.close()


def _watermark() -> str:
    """A cheap change token: DB file size + mtime of the -wal and main file."""
    parts = []
    for suffix in ("", "-wal"):
        p = Path(DB_PATH + suffix)
        if p.exists():
            stt = p.stat()
            parts.append(f"{stt.st_size}:{int(stt.st_mtime_ns)}")
    return "|".join(parts)


@app.get("/api/snapshot")
def get_snapshot(book: str = "live") -> JSONResponse:
    err = _book_err(book)
    if err is not None:
        return err
    snap = _snapshot(_db_for(book))
    snap["meta"]["book"] = book
    return JSONResponse(snap)


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "db": DB_PATH, "db_exists": Path(DB_PATH).exists()}


@app.get("/api/history")
def get_history(scope: str = "live", limit: int = 200, book: str = "live") -> JSONResponse:
    """Trade history beyond the snapshot's live slice (closed trades + expired watchers)."""
    if scope not in ("live", "seed", "all"):
        return JSONResponse({"error": 'scope must be "live", "seed" or "all"'}, status_code=422)
    if not 1 <= limit <= 1000:
        return JSONResponse({"error": "limit must be within [1, 1000]"}, status_code=422)
    err = _book_err(book)
    if err is not None:
        return err
    st = data.open_state(_db_for(book))
    try:
        rows = data.trade_history(st, scope, limit)
    finally:
        st.close()
    return JSONResponse({"scope": scope, "limit": limit, "rows": rows})


@app.get("/api/stream")
def get_stream(scope: str = "live", limit: int = 120, book: str = "live") -> JSONResponse:
    """The raw execution feed: every order/event, newest first, FDV-enriched.

    FDV at the executed moment = event price x token supply. Supply is effectively
    constant for these tokens, so one cached DexScreener lookup per mint (keyless,
    small per-request budget) prices every historical event correctly."""
    if scope not in ("live", "seed", "all"):
        return JSONResponse({"error": 'scope must be "live", "seed" or "all"'}, status_code=422)
    if not 1 <= limit <= 500:
        return JSONResponse({"error": "limit must be within [1, 500]"}, status_code=422)
    err = _book_err(book)
    if err is not None:
        return err
    st = data.open_state(_db_for(book))
    try:
        rows = data.exec_stream(st, scope, limit)
    finally:
        st.close()
    warm_budget = _SUPPLY_WARM_PER_REQ
    for r in rows:
        supply, known = _supply_cached(r["mint"])
        if not known and warm_budget > 0:
            warm_budget -= 1
            _supply_warm(r["mint"])          # F36: warm in the background, never block the request
        r["fdv_usd"] = round(r["price"] * supply) if (supply and r["price"]) else None
    return JSONResponse({"scope": scope, "rows": rows})


@app.get("/api/lab/{config_id}")
def get_lab_config(config_id: str, book: str = "live") -> JSONResponse:
    err = _book_err(book)
    if err is not None:
        return err
    st = data.open_state(_db_for(book))
    try:
        detail = data.lab_config_detail(st, config_id)
    finally:
        st.close()
    if detail is None:
        return JSONResponse({"error": "unknown config"}, status_code=404)
    return JSONResponse(detail)


@app.get("/api/token/{mint}")
def get_token(mint: str, book: str = "live") -> JSONResponse:
    err = _book_err(book)
    if err is not None:
        return err
    st = data.open_state(_db_for(book))
    try:
        detail = data.token_detail(st, mint)
    finally:
        st.close()
    if detail is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(detail)


@app.websocket("/ws")
async def ws(websocket: WebSocket) -> None:
    await websocket.accept()
    last = None
    try:
        # initial push. The WS only ever carries the LIVE book — stamp it so the client can drop a
        # frame that would render live data under a PAPER view (or vice versa) after a book toggle.
        snap = await asyncio.to_thread(_snapshot)
        snap["meta"]["book"] = "live"
        await websocket.send_json({"type": "snapshot", "payload": snap})
        last = _watermark()
        beats = 0
        while True:
            await asyncio.sleep(2.0)
            wm = _watermark()
            if wm != last:
                last = wm
                snap = await asyncio.to_thread(_snapshot)
                snap["meta"]["book"] = "live"
                await websocket.send_json({"type": "snapshot", "payload": snap})
            else:
                beats += 1
                await websocket.send_json({"type": "heartbeat", "ts": beats})
    except WebSocketDisconnect:
        return
    except Exception:
        return


# --------------------------------------------------------------------------- #
# Runtime control plane. ONLY the keys below are editable (plus the two strategy-lab keys
# validated inline in post_control: research_requested='1', champion_config_id=C1..C10).
# The strategy parameters
# (dip / stop / TP ladder / re-entry) are research-locked — there is NO write path
# for them here, and `mode` is never settable from the UI (live arming is CLI-gated).
# ctl_stake_usd max == STAKE_HARD_CAP_USD: stage39 measured $10-fixed survives, $25 busts to $0.
_CTL_BOUNDS: dict[str, tuple[float, float]] = {
    "ctl_stake_usd": (0.5, STAKE_HARD_CAP_USD),
    "ctl_max_concurrent": (1, 100),
    "ctl_total_deployed_cap_usd": (10, 1000),
    "ctl_daily_loss_cap_usd": (5, 500),
    # MANUAL layer caps (pure system_state; not config.toml): total manual exposure ceiling and
    # the per-order fat-finger clamp on a single manual buy.
    "manual_cap_usd": (0, 1000),
    "manual_trade_hard_cap_usd": (0.5, STAKE_HARD_CAP_USD),
}
# ctl key -> (config.toml [strategy.tailrider] key, fallback default)
_CTL_CFG_KEYS: dict[str, tuple[str, float]] = {
    "ctl_stake_usd": ("stake_usd", 3.0),
    "ctl_max_concurrent": ("max_concurrent", 25),
    "ctl_total_deployed_cap_usd": ("total_deployed_cap_usd", 200.0),
    "ctl_daily_loss_cap_usd": ("daily_loss_cap_usd", 50.0),
    "manual_cap_usd": ("manual_cap_usd", 50.0),
    "manual_trade_hard_cap_usd": ("manual_trade_hard_cap_usd", 10.0),
}

_LOCKED_STRATEGY = {
    "dip_trigger": 0.50,
    "stop_level_mult": 0.70,
    "tp1": "3x sell 33% then remove stop",
    "ladder": "6/12/24/48x (x2) then x3",
    "reentry": False,
    "note": "locked by research — editing these invalidates the backtest equivalence",
}


def _ctl_defaults() -> dict[str, float]:
    t = Settings.load().raw.get("strategy", {}).get("tailrider", {}) or {}
    return {ctl: float(t.get(cfg_key, fb)) for ctl, (cfg_key, fb) in _CTL_CFG_KEYS.items()}


@app.get("/api/control")
def get_control() -> JSONResponse:
    defaults = _ctl_defaults()
    st = data.open_state(DB_PATH)
    try:
        kill = st.get_system("kill_switch", "off")
        mode = st.get_system("mode", "paper")
        current = {k: st.get_system(k) for k in _CTL_BOUNDS}
    finally:
        st.close()
    editable: dict = {"kill_switch": {"value": kill}}
    for key, (lo, hi) in _CTL_BOUNDS.items():
        value = float(current[key]) if current[key] is not None else defaults[key]
        if key == "ctl_stake_usd":
            value = min(value, STAKE_HARD_CAP_USD)   # effective stake is always clamped
        editable[key] = {"value": value, "min": lo, "max": hi, "default": defaults[key]}
    return JSONResponse({
        "editable": editable,
        "locked": _LOCKED_STRATEGY,
        "mode": mode,
        "mode_note": "live arming is CLI-gated (MEMEBOT_LIVE_ARMED), never a UI toggle",
    })


_custom_lock = threading.Lock()   # serialize read-modify-write of the custom set


def _custom_challenger_control(action: str, value) -> JSONResponse:
    """add_challenger / delete_challenger: mutate the custom set in system_state.
    The engine polls custom_challengers_rev and races new strategies forward-only."""
    from memebot.live.shadow import CUSTOM_KEY, CUSTOM_REV_KEY, MAX_CUSTOM, challenger_from_dict
    from memebot.live.state import LiveState
    with _custom_lock:
        st = LiveState(DB_PATH)
        try:
            try:
                existing = json.loads(st.get_system(CUSTOM_KEY) or "[]")
            except Exception:
                existing = []
            if action == "add_challenger":
                if not isinstance(value, dict):
                    return JSONResponse({"error": "value must be the strategy definition object"},
                                        status_code=422)
                if len(existing) >= MAX_CUSTOM:
                    return JSONResponse({"error": f"at most {MAX_CUSTOM} custom strategies"},
                                        status_code=422)
                # MONOTONIC id counter — X-ids are never reused, so a deleted strategy's
                # stray rows can never be inherited by a successor.
                floor = 1 + max((int(d["id"][1:]) for d in existing
                                 if str(d.get("id", "")).startswith("X")), default=0)
                next_n = max(int(st.get_system("custom_next_id") or "1"), floor)
                value = dict(value, id=f"X{next_n}")
                try:
                    cc = challenger_from_dict(value)
                except ValueError as e:
                    return JSONResponse({"error": str(e)}, status_code=422)
                # F37: persist a CLEAN projection of the validated Challenger, not the raw
                # client dict — otherwise extra/oversized keys survive validation and get
                # re-parsed on every load/snapshot/rev-poll.
                clean = {"id": cc.id, "label": cc.label, "dip": cc.dip, "sl": cc.sl,
                         "ftp": cc.ftp, "fsell": cc.fsell, "reentry": cc.reentry,
                         "entry_mode": cc.entry_mode}
                existing.append(clean)
                st.set_system("custom_next_id", str(next_n + 1))
                result = {"ok": True, "id": cc.id, "label": cc.label}
            else:                                   # delete_challenger
                cid = str(value or "")
                if not (cid.startswith("X") and any(str(d.get("id")) == cid for d in existing)):
                    return JSONResponse({"error": f"unknown custom strategy {cid!r} "
                                         "(built-ins C1-C18 cannot be deleted)"}, status_code=422)
                existing = [d for d in existing if str(d.get("id")) != cid]
                # its race history goes with it — riders AND closed legs. The engine prunes
                # its in-memory riders on the next rev poll (~30s) and repeats this cleanup,
                # catching any row a still-racing rider re-upserted in the window.
                st.delete_shadow_config(cid)
                result = {"ok": True, "id": cid, "deleted": True}
            st.set_system(CUSTOM_KEY, json.dumps(existing))
            st.set_system(CUSTOM_REV_KEY, str(int(st.get_system(CUSTOM_REV_KEY) or "0") + 1))
            return JSONResponse(result)
        finally:
            st.close()


@app.post("/api/control")
def post_control(body: dict) -> JSONResponse:
    key = body.get("key")
    value = body.get("value")
    if key in ("add_challenger", "delete_challenger"):
        return _custom_challenger_control(key, value)
    if key == "kill_switch":
        if value not in ("on", "off"):
            return JSONResponse({"error": 'kill_switch must be "on" or "off"'}, status_code=422)
    elif key == "research_requested":
        # one-shot flag: the engine consumes it and clears the key; only '1' is valid
        if value != "1":
            return JSONResponse({"error": 'research_requested accepts only "1"'}, status_code=422)
    elif key == "champion_config_id":
        # F38: champion promotion is NOT yet wired to the engine — nothing in
        # engine/strategy/executor/run reads champion_config_id, so the live engine always
        # trades config #1 (C1). Accepting this write would set a key the dashboard then
        # displays as "the champion" while the engine trades something else — a lie. Reject
        # it until real engine support lands (the seeded default stays C1).
        return JSONResponse(
            {"error": "champion promotion is not yet wired to the engine (it trades C1); "
                      "this control is disabled until engine support lands"},
            status_code=422)
    elif key in _CTL_BOUNDS:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return JSONResponse({"error": f"{key} must be a number"}, status_code=422)
        lo, hi = _CTL_BOUNDS[key]
        if not lo <= value <= hi:
            return JSONResponse(
                {"error": f"{key} must be within [{lo}, {hi}], got {value}"}, status_code=422)
        value = int(value) if key == "ctl_max_concurrent" else float(value)
    else:
        return JSONResponse({"error": f"unknown or non-editable key: {key!r}"}, status_code=400)
    from memebot.live.state import LiveState  # writable open (idempotent DDL); brief, then close
    st = LiveState(DB_PATH)
    try:
        st.set_system(key, str(value))
    finally:
        st.close()
    return JSONResponse({"ok": True, "key": key, "value": value})


# --------------------------------------------------------------------------- #
# MANUAL trading control plane. The dashboard WRITES orders / watchlist / controller here; the
# ENGINE (a SEPARATE process) reads those rows and executes every order through the SAME safe
# money path as the algo (confirm-then-commit, idempotent sells, breaker, burner allowlist, arming
# gates). So these routes are validation + persistence only — they never touch a wallet. The
# engine re-checks arming/kill/caps at fire time; the checks here are for immediate UX feedback.
# All mutating routes are behind BasicAuth (the middleware).
_ORDER_KINDS = {"market", "limit", "take_profit", "stop_loss", "trailing_stop"}
_TRIGGER_TYPES = {"now", "price_at_or_below", "price_at_or_above", "mult_at_or_above",
                  "peak_drawdown_pct"}
_SIZE_KINDS = {"usd", "token_frac", "token_abs"}
_ACTIVE_STATES = ("ENTERED", "SECURED", "RIDING")


def _err(msg: str, code: int = 422) -> JSONResponse:
    return JSONResponse({"error": msg}, status_code=code)


# audit #35: a Solana mint is base58 (no 0/O/I/l), 32-44 chars. Reject non-base58 / wrong-length junk
# so it can never become a live WATCHING row (the parser is the live entry authority downstream).
_BASE58_MINT = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


def _valid_mint(m) -> bool:
    return isinstance(m, str) and bool(_BASE58_MINT.match(m))


def _numv(v):
    # H2: reject non-finite (Infinity/NaN) too — json.loads('1e400') -> inf would otherwise pass
    # every validator and poison closed_trades / permanently 500 the snapshot.
    return (v if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
            else None)


@app.post("/api/manual/order")
def post_manual_order(body: dict) -> JSONResponse:
    from memebot.live.state import LiveState
    # book: "live" places a REAL order (engine executes it); "paper" places a PRACTICE order the
    # paper twin fills with simulated money — full functionality, zero risk (user request 2026-07-06)
    book = str(body.get("book") or "live")
    berr = _book_err(book)
    if berr is not None:
        return berr
    mint, side, kind = body.get("mint"), body.get("side"), body.get("kind")
    trigger_type = body.get("trigger_type")
    trigger_value = _numv(body.get("trigger_value"))
    size_kind, size_value = body.get("size_kind"), _numv(body.get("size_value"))
    ticker = body.get("ticker")
    ticker = str(ticker)[:32] if ticker is not None else None    # L4: tolerate non-string ticker
    note = str(body.get("note") or "")[:200]                     # L4: tolerate non-string note
    expires_h = _numv(body.get("expires_h"))
    if expires_h is not None and expires_h > 8760:      # audit #35: bound the one unbounded numeric
        return _err("expires_h must be <= 8760 (1 year)")   # input -> timedelta OverflowError -> 500
    if not _valid_mint(mint):
        return _err("invalid mint address")
    if side not in ("buy", "sell"):
        return _err('side must be "buy" or "sell"')
    if kind not in _ORDER_KINDS:
        return _err(f"kind must be one of {sorted(_ORDER_KINDS)}")
    if size_kind not in _SIZE_KINDS:
        return _err(f"size_kind must be one of {sorted(_SIZE_KINDS)}")
    if size_value is None or size_value <= 0:
        return _err("size_value must be > 0")
    if kind == "market":
        trigger_type, trigger_value = "now", None
    elif trigger_type not in _TRIGGER_TYPES:
        return _err(f"trigger_type must be one of {sorted(_TRIGGER_TYPES)}")
    elif trigger_type == "peak_drawdown_pct":
        if not (0 < (trigger_value or 0) < 1):
            return _err("trailing drawdown must be a fraction in (0, 1)")
    elif trigger_value is None or trigger_value <= 0:
        return _err("trigger_value must be > 0")
    st = LiveState(_db_for(book))
    try:
        pos = st.get_position(mint)
        if side == "buy":
            if size_kind != "usd":
                return _err("buys must size in usd")
            hard = float(st.get_system("manual_trade_hard_cap_usd") or 0)
            if hard > 0:
                size_value = min(size_value, hard)
            size_value = min(size_value, STAKE_HARD_CAP_USD)   # survival ceiling (engine re-clamps)
            if pos and pos["state"] in _ACTIVE_STATES:
                return _err("already holding this position", 409)
            cap = float(st.get_system("manual_cap_usd") or 0)
            if cap <= 0:                                          # cap of 0 = direct buys disabled
                return _err("direct buys are disabled (manual cap is 0)", 409)
            if kind == "market" and st.get_system("kill_switch") == "on":
                return _err("kill-switch is on (new buys blocked)", 409)
        else:                                            # sell — an OVERRIDE of whatever manages it
            if size_kind == "usd":
                return _err("sells size in token_frac or token_abs")
            if size_kind == "token_frac" and not (0 < size_value <= 1):
                return _err("token_frac must be in (0, 1]")
            if not pos or pos["state"] not in _ACTIVE_STATES:
                return _err("no open position to act on", 409)
        expires_at = None
        if expires_h and expires_h > 0:
            expires_at = datetime.now(timezone.utc) + timedelta(hours=expires_h)
        elif kind == "market":
            expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)   # don't rest forever
        oid = st.create_order(
            mint=mint, ticker=ticker or (pos.get("ticker") if pos else None), kind=kind, side=side,
            trigger_type=trigger_type, trigger_value=trigger_value, size_kind=size_kind,
            size_value=size_value, note=note, expires_at=expires_at,
            position_id=(pos["id"] if pos else None))
        # audit #19: implicit take-over for a SELL override runs AFTER the order insert succeeds
        # (b — a failed insert must not leave a controller='manual', order-less, stop-less position).
        # And when taking over an UNSECURED position the algo rider is the ONLY holder of the -30% hard
        # stop; carry a protective stop_loss at the locked level before the rider is dropped (a), unless
        # this order is itself a stop.
        if side == "sell" and pos and pos.get("controller") != "manual":
            if (pos["state"] == "ENTERED" and not pos.get("secured") and pos.get("stop_price")
                    and kind not in ("stop_loss", "trailing_stop")):
                st.create_order(
                    mint=mint, ticker=pos.get("ticker"), kind="stop_loss", side="sell",
                    trigger_type="price_at_or_below", trigger_value=pos["stop_price"],
                    size_kind="token_frac", size_value=1.0, position_id=pos["id"],
                    note="auto-carried -30% stop (take-over protection)")
            st.update_position(mint, controller="manual")
            st.set_system("controller_rev",
                          str(int(st.get_system("controller_rev") or "0") + 1))
        return JSONResponse({"ok": True, "id": oid, "size_value": size_value,
                             "mode": st.get_system("mode", "paper")})
    finally:
        st.close()


@app.get("/api/manual/orders")
def get_manual_orders(status: str = "open", book: str = "live") -> JSONResponse:
    err = _book_err(book)
    if err is not None:
        return err
    st = data.open_state(_db_for(book))
    try:
        rows = st.open_orders() if status == "open" else st.all_orders(200)
    finally:
        st.close()
    return JSONResponse({"orders": rows})


@app.delete("/api/manual/order/{order_id}")
def cancel_manual_order(order_id: int, book: str = "live") -> JSONResponse:
    from memebot.live.state import LiveState
    err = _book_err(book)
    if err is not None:
        return err
    st = LiveState(_db_for(book))
    try:
        o = st.get_order(order_id)
        if o is None:
            return _err("unknown order", 404)
        if o["status"] != "open":
            return _err(f"order is '{o['status']}' — only an open order can be cancelled", 409)
        st.update_order(order_id, status="cancelled", note="cancelled by user")
        return JSONResponse({"ok": True})
    finally:
        st.close()


@app.patch("/api/manual/order/{order_id}")
def modify_manual_order(order_id: int, body: dict, book: str = "live") -> JSONResponse:
    from memebot.live.state import LiveState
    err = _book_err(book)
    if err is not None:
        return err
    st = LiveState(_db_for(book))
    try:
        o = st.get_order(order_id)
        if o is None:
            return _err("unknown order", 404)
        if o["status"] != "open":
            return _err("only an open order can be modified", 409)
        fields: dict = {}
        tv, sv = _numv(body.get("trigger_value")), _numv(body.get("size_value"))
        if tv is not None:
            if o["trigger_type"] == "peak_drawdown_pct" and not (0 < tv < 1):
                return _err("trailing drawdown must be in (0, 1)")
            if o["trigger_type"] not in ("now", "peak_drawdown_pct") and tv <= 0:
                return _err("trigger_value must be > 0")
            fields["trigger_value"] = tv
        if sv is not None:
            if sv <= 0:
                return _err("size_value must be > 0")
            if o["side"] == "buy":
                hard = float(st.get_system("manual_trade_hard_cap_usd") or 0)
                if hard > 0:
                    sv = min(sv, hard)
            if o["size_kind"] == "token_frac" and not (0 < sv <= 1):
                return _err("token_frac must be in (0, 1]")
            fields["size_value"] = sv
        if not fields:
            return _err("nothing to modify (trigger_value / size_value)")
        st.update_order(order_id, **fields)
        return JSONResponse({"ok": True, **fields})
    finally:
        st.close()


@app.post("/api/watchlist")
def add_watchlist(body: dict) -> JSONResponse:
    from memebot.live.state import LiveState
    book = str(body.get("book") or "live")
    berr = _book_err(book)
    if berr is not None:
        return berr
    mint, ticker = body.get("mint"), body.get("ticker")
    ticker = str(ticker)[:32] if ticker is not None else None    # L4: tolerate non-string
    note = str(body.get("note") or "")[:200]
    if not _valid_mint(mint):
        return _err("invalid mint address")
    st = LiveState(_db_for(book))
    try:
        st.add_watch(mint, ticker=ticker, note=note)
        return JSONResponse({"ok": True, "mint": mint})
    finally:
        st.close()


@app.delete("/api/watchlist/{mint}")
def remove_watchlist(mint: str, book: str = "live") -> JSONResponse:
    from memebot.live.state import LiveState
    err = _book_err(book)
    if err is not None:
        return err
    st = LiveState(_db_for(book))
    try:
        st.remove_watch(mint)
        return JSONResponse({"ok": True})
    finally:
        st.close()


@app.post("/api/positions/{mint}/takeover")
def takeover_position(mint: str, book: str = "live") -> JSONResponse:
    """Transfer an algo position to the human. The engine drops its TailRider on the next sampler
    reconcile (≤30s) — take-over is deliberate, not time-critical — and the human's orders drive it."""
    from memebot.live.state import LiveState
    err = _book_err(book)
    if err is not None:
        return err
    st = LiveState(_db_for(book))
    try:
        pos = st.get_position(mint)
        if pos is None or pos["state"] not in _ACTIVE_STATES:
            return _err("no active position to take over", 404)
        if pos.get("controller") == "manual":
            return _err("already under manual control", 409)
        st.update_position(mint, controller="manual")
        st.set_system("controller_rev", str(int(st.get_system("controller_rev") or "0") + 1))
        st.record_alert(severity="INFO", kind="MANUAL_ORDER",
                        message=f"took over {pos.get('ticker') or mint[:6]}… (algo → manual)")
        return JSONResponse({"ok": True})
    finally:
        st.close()


@app.post("/api/positions/{mint}/release")
def release_position(mint: str, book: str = "live") -> JSONResponse:
    """Hand a manual position back to the algo. The engine rehydrates a TailRider from its current
    state on the next reconcile (the algo resumes as a config-#1 position from where it stands)."""
    from memebot.live.state import LiveState
    err = _book_err(book)
    if err is not None:
        return err
    st = LiveState(_db_for(book))
    try:
        pos = st.get_position(mint)
        if pos is None or pos["state"] not in _ACTIVE_STATES:
            return _err("no active position to release", 404)
        if pos.get("controller") != "manual":
            return _err("position is not under manual control", 409)
        st.update_position(mint, controller="algo")
        st.set_system("controller_rev", str(int(st.get_system("controller_rev") or "0") + 1))
        # its resting manual orders no longer apply once the algo drives it
        for o in st.open_orders(mint):
            st.update_order(o["id"], status="cancelled", note="released to algo")
        st.record_alert(severity="INFO", kind="MANUAL_ORDER",
                        message=f"released {pos.get('ticker') or mint[:6]}… (manual → algo)")
        return JSONResponse({"ok": True})
    finally:
        st.close()


@app.post("/api/signal")
def post_signal(body: dict) -> JSONResponse:
    """ADD WATCHLIST = inject a token as a CALL the algo trades (config #1). Fetches a live price
    for the anchor + immediate feedback; the engine loop picks up the pending row and creates the
    WATCHING position, exactly like a channel signal."""
    from memebot.live.state import LiveState
    book = str(body.get("book") or "live")
    berr = _book_err(book)
    if berr is not None:
        return berr
    mint, ticker = body.get("mint"), body.get("ticker")
    ticker = str(ticker)[:32] if ticker is not None else None
    if not _valid_mint(mint):
        return _err("invalid mint address")
    st = LiveState(_db_for(book))
    try:
        pos = st.get_position(mint)
        if pos and pos["state"] in ("WATCHING",) + _ACTIVE_STATES:
            return _err("already tracked", 409)
        try:
            obj = _price().price_full([mint]).get(mint) or {}
            price = obj.get("usdPrice")
        except Exception:
            price = None
        if not price or price <= 0:
            return _err("could not fetch a live price for this mint", 422)
        sid = st.add_manual_signal(mint, ticker=ticker, price=float(price), note="manual add")
        return JSONResponse({"ok": True, "id": sid, "price": float(price)})
    finally:
        st.close()


@app.get("/api/lookup/{mint}")
def lookup_token(mint: str) -> JSONResponse:
    """Paste-a-CA preview: ticker + price + FDV + liquidity for an arbitrary mint (best DexScreener
    pair). Used by the [EXTERNAL INPUT] popup to auto-fill stats before add/buy."""
    global _dex_client
    if not _valid_mint(mint):
        return _err("invalid mint address")
    out = {"mint": mint, "ticker": None, "price": None, "fdv": None, "liquidity": None}
    try:
        from memebot.data.dexscreener import DexScreenerClient
        with _clients_lock:
            if _dex_client is None:
                _dex_client = DexScreenerClient(timeout=5.0, max_retries=1)
        best_liq = -1.0
        for pair in _dex_client.token_pairs(mint):
            try:
                liq = float((pair.get("liquidity") or {}).get("usd") or 0)
                if liq > best_liq:
                    best_liq = liq
                    bt = pair.get("baseToken") or {}
                    out["ticker"] = bt.get("symbol")
                    out["price"] = float(pair.get("priceUsd") or 0) or None
                    out["fdv"] = pair.get("fdv")
                    out["liquidity"] = liq or None
            except (TypeError, ValueError):
                continue
    except Exception:
        pass
    return JSONResponse(out)


# --------------------------------------------------------------------------- #
# Market data for the token terminal view. KEYLESS clients ONLY — the trading
# engine owns the keyed api.jup.ag quota (60 rpm) and the dashboard must NEVER
# touch it: JupiterChartsClient hits datapi (min_interval=0.4) for candles,
# JupiterClient(api_key=None) hits the lite-api pool (min_interval=1.05) for spot.
# Every call goes through a small in-process TTL cache so modal-opens and
# refresh polling can never hammer anything. Constructed lazily (tests replace
# these module attributes with fakes).
_charts_client: JupiterChartsClient | None = None
_price_client: JupiterClient | None = None
_clients_lock = threading.Lock()


def _charts() -> JupiterChartsClient:
    global _charts_client
    with _clients_lock:
        if _charts_client is None:
            _charts_client = JupiterChartsClient(min_interval=0.4)
        return _charts_client


def _price() -> JupiterClient:
    global _price_client
    with _clients_lock:
        if _price_client is None:
            _price_client = JupiterClient(api_key=None, min_interval=1.05)
        return _price_client


# -- token supply cache (for STREAM FDV = event price x supply) ------------------- #
# DexScreener is keyless; supply is constant for these tokens so a long TTL is honest.
# A small per-request fetch budget means a cold cache warms over a few polls instead
# of stalling one request on 30 lookups.
_SUPPLY_TTL = 6 * 3600.0
_SUPPLY_WARM_PER_REQ = 8     # how many cold mints to background-warm per request (rest warm later)
_supply_cache: dict[str, tuple[float, float | None]] = {}   # mint -> (expiry, supply|None)
_dex_client = None
# F36: warm the supply cache OFF the request path — the stream route must never block on
# DexScreener. A small daemon pool fetches cold mints; the request serves whatever is cached
# (fdv=null until a mint warms, filled in on a later ~2s poll).
_supply_warm_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="supply-warm")
_supply_warming: set[str] = set()


def _supply_warm(mint: str) -> None:
    """Fire-and-forget background cache warm, deduped so a mint isn't queued twice."""
    with _cache_lock:
        if mint in _supply_warming:
            return
        _supply_warming.add(mint)

    def _job():
        try:
            _supply_fetch(mint)
        finally:
            with _cache_lock:
                _supply_warming.discard(mint)

    try:
        _supply_warm_pool.submit(_job)
    except Exception:
        with _cache_lock:
            _supply_warming.discard(mint)


def _supply_cached(mint: str) -> tuple[float | None, bool]:
    """(supply, known) — known=True means a fresh cache entry exists (even a failed one)."""
    with _cache_lock:
        hit = _supply_cache.get(mint)
        if hit and hit[0] > time.monotonic():
            return hit[1], True
    return None, False


def _supply_fetch(mint: str) -> float | None:
    global _dex_client
    # In-flight dedup: claim the cache slot BEFORE the (blocking) network call so
    # stacked stream polls never re-fetch the same cold mint concurrently.
    with _cache_lock:
        hit = _supply_cache.get(mint)
        if hit and hit[0] > time.monotonic():
            return hit[1]
        _supply_cache[mint] = (time.monotonic() + 60.0, None)
    supply = None
    try:
        from memebot.data.dexscreener import DexScreenerClient
        with _clients_lock:
            if _dex_client is None:
                _dex_client = DexScreenerClient(timeout=5.0, max_retries=1)
        best_liq = -1.0
        for pair in _dex_client.token_pairs(mint):
            try:
                fdv, px = pair.get("fdv"), float(pair.get("priceUsd") or 0)
                liq = float((pair.get("liquidity") or {}).get("usd") or 0)
                if fdv and px > 0 and liq > best_liq:
                    best_liq, supply = liq, float(fdv) / px
            except (TypeError, ValueError):
                continue
    except Exception:
        supply = None
    with _cache_lock:
        # cache failures too (short TTL) so a dead mint can't burn the budget every poll
        _supply_cache[mint] = (time.monotonic() + (_SUPPLY_TTL if supply else 300.0), supply)
    return supply


_CANDLES_TTL = 20.0                                              # seconds
_LIVE_TTL = 3.0                                                  # seconds
_cache_lock = threading.Lock()   # routes run in the threadpool — guard get/set
_candles_cache: dict[tuple[str, str, str], tuple[float, dict]] = {}
_live_cache: dict[str, tuple[float, dict]] = {}


def _cache_get(cache: dict, key):
    with _cache_lock:
        hit = cache.get(key)
        return hit[1] if hit and hit[0] > time.monotonic() else None


def _cache_set(cache: dict, key, payload: dict, ttl: float) -> None:
    with _cache_lock:
        cache[key] = (time.monotonic() + ttl, payload)


_DATAPI_INTERVAL = {"1m": "1_MINUTE", "1h": "1_HOUR", "1d": "1_DAY"}
# config #1's level structure — same 0.70 stop / 3/6/12/24/48 ladder token_detail uses
_RUNG_MULTS = (3.0, 6.0, 12.0, 24.0, 48.0)
_STOP_MULT = 0.70


@app.get("/api/token/{mint}/candles")
def get_candles(mint: str, range: str = "call", interval: str = "auto",
                book: str = "live") -> JSONResponse:
    if range not in ("call", "24h", "max"):
        return JSONResponse({"error": 'range must be "call", "24h" or "max"'}, status_code=422)
    if interval not in ("auto", "1m", "1h", "1d"):
        return JSONResponse({"error": 'interval must be "auto", "1m", "1h" or "1d"'},
                            status_code=422)
    err = _book_err(book)
    if err is not None:
        return err
    cached = _cache_get(_candles_cache, (book, mint, range, interval))
    if cached is not None:
        return JSONResponse(cached)

    st = data.open_state(_db_for(book))
    try:
        pos = st.get_position(mint)
    finally:
        st.close()
    if pos is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    signal_at = data._parse_ts(pos.get("signal_at"))
    if signal_at is None:
        return JSONResponse({"error": "position has no signal_at"}, status_code=404)

    now = datetime.now(timezone.utc)
    if range == "call":
        start = signal_at - timedelta(minutes=90)   # pre-roll: the call context stays visible
    elif range == "24h":
        start = now - timedelta(hours=24)
    else:  # max: from a week before the call, capped at a 30d span
        start = max(signal_at - timedelta(days=7), now - timedelta(days=30))
    end = now

    if interval == "auto":   # mirror JupiterChartsClient.get_price_series
        span = end - start
        eff = "1m" if span <= timedelta(hours=16) else (
            "1h" if span <= timedelta(days=40) else "1d")
    else:
        eff = interval

    try:
        candles = _charts().fetch_candles(mint, _DATAPI_INTERVAL[eff], start, end, candles=1000)
    except Exception:
        candles = []          # F35: datapi down/rate-limited -> 200 with empty candles + the
        # level overlay (below), mirroring get_live, instead of an unhandled 500.
    # LESSON (RESEARCH.md): datapi does NOT honor time bounds — clamp at the call
    # site or an unclamped fetch manufactures pre-signal history.
    candles = [c for c in candles if start <= c.ts <= end]

    signal_price = pos.get("signal_price")
    entry = pos.get("entry_price")
    stop = pos.get("stop_price")
    if stop is None and entry and not pos.get("secured"):
        stop = _STOP_MULT * entry            # unsecured entered position: the locked −30% stop
    payload = {
        "mint": mint,
        "interval": eff,
        "from": start.isoformat(),
        "to": end.isoformat(),
        "candles": [[c.ts.isoformat(), c.open, c.high, c.low, c.close, c.volume]
                    for c in candles],
        "levels": {
            "call": signal_price,
            "entry_gate": 0.5 * signal_price if signal_price else None,
            "entry": entry,
            "stop": stop,
            "rungs": [{"mult": m, "price": m * entry} for m in _RUNG_MULTS] if entry else [],
        },
    }
    _cache_set(_candles_cache, (book, mint, range, interval), payload, _CANDLES_TTL)
    return JSONResponse(payload)


@app.get("/api/token/{mint}/live")
def get_live(mint: str) -> JSONResponse:
    cached = _cache_get(_live_cache, mint)
    if cached is not None:
        return JSONResponse(cached)
    try:
        obj = _price().price_full([mint]).get(mint) or {}
    except Exception:
        obj = {}   # brand-new/unknown mint or a Jupiter hiccup -> 200 with nulls, never 500

    def _num(v):
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None

    payload = {
        "price": _num(obj.get("usdPrice")),
        "liquidity": _num(obj.get("liquidity")),
        "price_change_24h": _num(obj.get("priceChange24h")),
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    _cache_set(_live_cache, mint, payload, _LIVE_TTL)
    return JSONResponse(payload)


# Serve the built frontend last so /api and /ws take precedence.
if FRONTEND_DIST.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIST), html=True), name="frontend")
