"""Read-only SQLite -> plain dicts. The single shared read layer (no web framework here).

Builds the dashboard snapshot: meta, the power-law hero points (every position's multiple +
Pareto), the equity curves (chosen $-fixed vs survivable fractional reference), lifetime stats
(honest: win%, concentration, bleed/total-loss rates, days-since-≥10x, Hill alpha), open
positions, the signal feed, and alerts. Honest-by-design: a normal bleed reads as "as designed".
"""

from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from memebot.live.state import LiveState, from_iso

DEFAULT_DB = Path(__file__).resolve().parents[1] / "runs" / "live_state.db"
# the paper twin (measurement book) — same schema, separate file; see run.py "PAPER TWIN"
DEFAULT_PAPER_DB = Path(__file__).resolve().parents[1] / "runs" / "paper_state.db"
LOSS_FLOOR = 0.01          # log-axis floor for total-loss (0x) points
TAIL_X = 10.0              # "the tail" threshold


def open_state(db_path: str | Path = DEFAULT_DB) -> LiveState:
    return LiveState(db_path, read_only=True)


# --------------------------------------------------------------------------- #
def _parse_ts(s: Optional[str]) -> Optional[datetime]:
    """ISO text -> aware UTC datetime (naive treated as UTC); None/garbage -> None."""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _is_live(ts_iso: Optional[str], seeded_at: Optional[str]) -> bool:
    """LIVE vs SEED discrimination (no schema change): system_state['seeded_at'] holds the
    seed timestamp; a positions/closed_trades row stamped (opened_at/exit_at) strictly AFTER
    it is LIVE forward-paper activity, at/before it is the backtest SEED replay.
    If seeded_at is absent (never seeded), everything counts as live."""
    if not seeded_at:
        return True
    seed = _parse_ts(seeded_at)
    if seed is None:
        return True
    ts = _parse_ts(ts_iso)
    if ts is None:
        return False
    return ts > seed


def _live_mints(state: LiveState) -> Optional[set]:
    """The set of mints traded by the LIVE engine — classified by PROVENANCE, not time.

    Timestamps leak across the seed boundary in both directions (historical exit candles
    post-date the stamp by hours; the corpus itself contains signals posted after the stamp;
    seed rows' opened_at is script wall time — each of these misfiled seed trades as "live"
    on 2026-07-03). The reliable marker already in the DB: the seed script writes
    seen_mints.outcome='seen' (its default), while the live engine ALWAYS writes
    'positioned' or 'rejected'. Mints absent from seen_mints fall back to the signal_at
    timestamp rule. Returns None when never seeded -> everything counts as live."""
    seeded_at = state.get_system("seeded_at")
    if not seeded_at:
        return None
    rows = state.query(
        "SELECT p.mint AS mint, p.signal_at AS signal_at, s.outcome AS outcome "
        "FROM positions p LEFT JOIN seen_mints s ON s.mint = p.mint")
    out: set = set()
    for r in rows:
        if r["outcome"] is None:
            if _is_live(r["signal_at"], seeded_at):
                out.add(r["mint"])
        elif r["outcome"] != "seen":
            out.add(r["mint"])
    return out


def _mint_is_live(mint: str, live_mints: Optional[set]) -> bool:
    return live_mints is None or mint in live_mints


def _controller_map(state: LiveState) -> dict:
    """{mint: controller}, defensively — a DB opened read-only before the engine added the
    positions.controller column (L5) yields an empty map, so everything reads as 'algo' rather
    than 500-ing the whole snapshot."""
    try:
        return {r["mint"]: (r["controller"] or "algo") for r in
                state.query("SELECT mint, controller FROM positions")}
    except sqlite3.OperationalError:
        return {}


def _hill_alpha(mults: list[float], tail_frac: float = 0.10) -> Optional[float]:
    a = sorted((m for m in mults if m and m > 0), reverse=True)
    k = max(5, int(len(a) * tail_frac))
    if len(a) <= k:
        return None
    denom = sum(math.log(a[i] / a[k]) for i in range(k)) / k
    return (1.0 / denom) if denom > 0 else None


def _now(state: LiveState) -> datetime:
    # wall clock, always — once the engine trades forward, "days since ≥10x" must be true time
    return datetime.now(timezone.utc)


def hero_points(state: LiveState) -> list[dict]:
    """One entry per position that has a multiple (realized closed + live open)."""
    lm = _live_mints(state)
    pts: list[dict] = []
    for c in state.closed_trades():
        pts.append({
            "mint": c["mint"], "ticker": c["ticker"] or c["mint"][:4],
            "multiple": c["realized_multiple"], "kind": "realized",
            "source": "live" if _mint_is_live(c["mint"], lm) else "seed",
            "pnl": c["pnl_usd"] or 0.0, "stake": c["stake_usd"] or 0.0,
            "state": "STOPPED" if c["was_stopped"] else "EXITED",
            "n_tp": c["n_tp"], "peak_multiple": c["peak_multiple"],
            "entry_at": c["entry_at"], "exit_at": c["exit_at"], "held_hours": c["held_hours"],
        })
    for p in state.active_positions():
        if p["state"] in ("ENTERED", "SECURED", "RIDING") and p["current_multiple"] is not None:
            stake = p["stake_usd"] or 0.0
            entry, peak = p["entry_price"], p["peak_price"]
            # incident lesson (2026-07-07): cash a position's sell legs have ALREADY returned is not a
            # mark — banked/stake is a realized FLOOR the tiles may count (see _scope_stats).
            banked = state.query(
                "SELECT COALESCE(SUM(proceeds_usd),0) AS b FROM position_events "
                "WHERE position_id=? AND event_type IN "
                "('TP','RIDE_SELL','STOP_OUT','FINALIZE','MANUAL_SELL')", (p["id"],))[0]["b"]
            pts.append({
                "mint": p["mint"], "ticker": p["ticker"] or p["mint"][:4],
                "multiple": p["current_multiple"], "kind": "unrealized",
                "source": "live" if _mint_is_live(p["mint"], lm) else "seed",
                "pnl": stake * (p["current_multiple"] - 1.0), "stake": stake,
                "banked_multiple": round(banked / stake, 4) if stake else None,
                "state": p["state"], "n_tp": p["n_tp"],
                # F52: a true multiple (peak/entry), matching the closed branch — NOT the raw price
                "peak_multiple": round(peak / entry, 3) if (entry and peak) else None,
                "entry_at": p["entry_at"], "exit_at": None, "held_hours": None,
            })
    return pts


def _pareto(points: list[dict]) -> list[dict]:
    """Rank by multiple desc; cumulative % of gross POSITIVE P&L (the concentration curve)."""
    ranked = sorted(points, key=lambda p: p["multiple"], reverse=True)
    gross = sum(p["pnl"] for p in ranked if p["pnl"] > 0) or 1.0
    cum = 0.0
    out = []
    for i, p in enumerate(ranked, start=1):
        if p["pnl"] > 0:
            cum += p["pnl"]
        out.append({"rank": i, "mint": p["mint"], "ticker": p["ticker"],
                    "multiple": p["multiple"], "pnl": p["pnl"], "state": p["state"],
                    "kind": p["kind"], "source": p.get("source", "live"),
                    "cum_pct": round(100.0 * cum / gross, 3)})
    return out


def account(state: LiveState) -> dict:
    """The headline account block: what the bankroll IS right now, LIVE activity only.

    Seed-replay rows are excluded via _is_live; with zero live activity this honestly
    reads balance_usd == start_usd — nothing has actually been traded forward yet."""
    seeded_at = state.get_system("seeded_at")
    lm = _live_mints(state)
    start = float(state.get_system("bankroll_start_usd", "500") or 500)
    now = _now(state)
    today = now.date().isoformat()

    live_closed = [c for c in state.closed_trades() if _mint_is_live(c["mint"], lm)]
    live_realized = sum((c["pnl_usd"] or 0.0 for c in live_closed), 0.0)
    closed_today = [c for c in live_closed if (c["exit_at"] or "")[:10] == today]
    today_realized = sum((c["pnl_usd"] or 0.0 for c in closed_today), 0.0)

    open_rows = state.query(
        "SELECT id,mint,stake_usd,current_multiple,realized_pnl_usd,unrealized_pnl_usd,"
        "remaining_frac,opened_at,tokens_qty,current_price "
        "FROM positions WHERE state IN ('ENTERED','SECURED','RIDING')")
    live_open = [p for p in open_rows if _mint_is_live(p["mint"], lm)]
    # F43: config #1 sells 33% at 3x to RECOVER the stake while the position stays open. So
    # "deployed" is the remaining cost basis (stake * remaining_frac), not the full original
    # stake — a secured position that already returned its cash no longer ties up dry powder.
    deployed = sum(((p["stake_usd"] or 0.0) * (p["remaining_frac"] if p["remaining_frac"]
                    is not None else 1.0) for p in live_open), 0.0)
    # M9 (audit 2026-07-07): the banked part of an open position comes from its EVENTS (real
    # USD received), not the rider's modeled proceeds_units — a $0-skip/clamped leg otherwise
    # overstates the balance until close (the exact divergence the incident repair hand-patched).
    banked_by_pid = {r["position_id"]: float(r["b"] or 0.0) for r in state.query(
        "SELECT position_id, COALESCE(SUM(proceeds_usd),0) AS b FROM position_events "
        "WHERE event_type IN ('TP','RIDE_SELL','STOP_OUT','FINALIZE','MANUAL_SELL') "
        "GROUP BY position_id")}
    unrealized = 0.0
    for p in live_open:
        stake = p["stake_usd"] or 0.0
        qty = p["tokens_qty"] or 0.0
        rem = p["remaining_frac"] if p["remaining_frac"] is not None else 1.0
        px = p["current_price"]
        if qty > 0 and px is not None:
            unrealized += banked_by_pid.get(p["id"], 0.0) + rem * qty * px - stake
        elif p["current_multiple"] is not None:
            # current_multiple = (pr + rem*price)/entry -> total mark-to-market vs stake
            unrealized += stake * (p["current_multiple"] - 1.0)
        else:
            unrealized += (p["realized_pnl_usd"] or 0.0) + (p["unrealized_pnl_usd"] or 0.0)

    n_watching = sum(1 for p in state.query("SELECT mint FROM positions WHERE state='WATCHING'")
                     if _mint_is_live(p["mint"], lm))

    live_since = seeded_at
    if not live_since:
        first_sig = state.query("SELECT ts FROM signals ORDER BY ts LIMIT 1")
        live_since = first_sig[0]["ts"] if first_sig else None

    ctl = state.get_system("ctl_stake_usd")
    return {
        "balance_usd": round(start + live_realized + unrealized, 2),   # THE number
        "start_usd": start,
        "live_realized_pnl": round(live_realized, 2),
        "live_unrealized_pnl": round(unrealized, 2),
        "deployed_usd": round(deployed, 2),
        "dry_powder_usd": round(start + live_realized - deployed, 2),
        "today_pnl_usd": round(today_realized, 2),
        "today_pnl_basis": "realized_only",   # honest: no cheap prior-day mark to diff against
        "n_closed_today": len(closed_today),
        "n_live_trades_closed": len(live_closed),
        "n_live_open": len(live_open),
        "n_live_watching": n_watching,
        "live_since": live_since,
        "stake_usd": float(ctl) if ctl else 3.0,
    }


def _scope_stats(points: list[dict], closed_rows: list[dict], now: datetime) -> dict:
    """The per-scope (live/seed/all) metric set. OUTCOME metrics (win%, mean, best, bleed,
    concentration, hill_alpha) are computed over CLOSED/realized points ONLY — an open bag
    transiently marked >1x is not a WIN yet (F42), and this keeps `best` consistent with
    `days_since_last_10x` (F44). The full mixed `points` set is used only by the hero chart
    + CCDF, which intentionally display live bags. `n` is the closed count; `n_open` is split
    out so the tile denominator can be labelled accurately."""
    closed_pts = [p for p in points if p.get("kind") == "realized"]
    # incident lesson (2026-07-07): banked cash is not a mark. An OPEN position whose SELL legs have
    # already returned >= its stake contributes its banked-so-far multiple as a realized FLOOR
    # (the number can only grow from there; the remaining bag rides on top). Without this the
    # tiles said "best 0.71x / 0 wins" while ~38x the stake sat REALIZED in the wallet. F42's
    # concern (a transient open MARK is not a win) is untouched — marks still don't count.
    banked_pts = [dict(p, multiple=p["banked_multiple"],
                       pnl=(p.get("stake") or 0.0) * (p["banked_multiple"] - 1.0))
                  for p in points
                  if p.get("kind") == "unrealized" and (p.get("banked_multiple") or 0) >= 1.0]
    closed_pts = closed_pts + banked_pts
    mults = [p["multiple"] for p in closed_pts]
    n = len(mults)
    out = {"n": n, "n_banked": len(banked_pts),
           "n_open": len(points) - n, "win_rate": None, "mean": None,
           "mean_ex_tail": None, "best": None, "hill_alpha": None,
           "top1_pnl_pct": None, "top3_pnl_pct": None, "bleed_rate": None,
           "total_loss_rate": None, "days_since_last_10x": None}
    last_10x = None
    for c in closed_rows:
        if (c["realized_multiple"] or 0) >= TAIL_X:
            ts = _parse_ts(c["exit_at"])
            if ts and (last_10x is None or ts > last_10x):
                last_10x = ts
    if last_10x:
        out["days_since_last_10x"] = round((now - last_10x).total_seconds() / 86400.0, 1)
    if n == 0:
        return out
    wins = sum(1 for m in mults if m > 1)
    srt = sorted(mults, reverse=True)
    mean = sum(mults) / n
    ex_tail = (sum(srt[1:]) / (n - 1)) if n > 1 else mean          # drop the single biggest
    gross_gains = sum(p["pnl"] for p in closed_pts if p["pnl"] > 0) or 1.0
    by_pnl = sorted(closed_pts, key=lambda p: p["pnl"], reverse=True)
    top1 = sum(p["pnl"] for p in by_pnl[:1] if p["pnl"] > 0)
    top3 = sum(p["pnl"] for p in by_pnl[:3] if p["pnl"] > 0)
    out.update({
        "win_rate": round(wins / n, 4),
        "mean": round(mean, 4),
        "mean_ex_tail": round(ex_tail, 4),
        "best": round(srt[0], 2),
        "hill_alpha": round(_hill_alpha(mults), 3) if _hill_alpha(mults) else None,
        "top1_pnl_pct": round(100 * top1 / gross_gains, 1),
        "top3_pnl_pct": round(100 * top3 / gross_gains, 1),
        "bleed_rate": round(sum(1 for m in mults if m < 1) / n, 4),
        "total_loss_rate": round(sum(1 for m in mults if m < 0.1) / n, 4),
    })
    return out


def stats(state: LiveState, points: list[dict]) -> dict:
    """Lifetime stats, restructured into live/seed/all scopes (points carry "source").

    The pre-existing top-level keys are kept, computed over "all", so the current frontend
    keeps working until its scope-aware update lands (BACKWARD COMPATIBLE)."""
    now = _now(state)
    lm = _live_mints(state)
    closed = state.closed_trades()
    live_closed = [c for c in closed if _mint_is_live(c["mint"], lm)]
    seed_closed = [c for c in closed if not _mint_is_live(c["mint"], lm)]
    live_pts = [p for p in points if p.get("source", "live") == "live"]
    seed_pts = [p for p in points if p.get("source", "live") == "seed"]
    scopes = {
        "live": _scope_stats(live_pts, live_closed, now),
        "seed": _scope_stats(seed_pts, seed_closed, now),
        "all": _scope_stats(points, closed, now),
    }

    mults = [p["multiple"] for p in points]
    n = len(mults)
    if n == 0:
        return {"n_positions": 0, "as_designed": True, **scopes}
    wins = sum(1 for m in mults if m > 1)
    srt = sorted(mults, reverse=True)
    best = srt[0]
    mean = sum(mults) / n
    ex_tail = (sum(srt[1:]) / (n - 1)) if n > 1 else mean          # drop the single biggest
    gross_gains = sum(p["pnl"] for p in points if p["pnl"] > 0) or 1.0
    by_pnl = sorted(points, key=lambda p: p["pnl"], reverse=True)
    top1 = sum(p["pnl"] for p in by_pnl[:1] if p["pnl"] > 0)
    top3 = sum(p["pnl"] for p in by_pnl[:3] if p["pnl"] > 0)
    realized_pnl = sum(p["pnl"] for p in points if p["kind"] == "realized")
    # audit #21: judge "as designed" over the REALIZED subset only. Computing it over open+closed lets
    # a single transient open mark >1x spuriously flip the honesty banner OFF (the guardrail must react
    # to the realized distribution, not unrealized noise). Default True until there is realized history.
    r_mults = [p["multiple"] for p in points if p["kind"] == "realized"]
    rn = len(r_mults)
    if rn >= 1:
        r_wins = sum(1 for m in r_mults if m > 1)
        r_srt = sorted(r_mults, reverse=True)
        r_ex_tail = (sum(r_srt[1:]) / (rn - 1)) if rn > 1 else r_srt[0]
        as_designed = (r_ex_tail < 1.0 and r_wins / rn < 0.25)
    else:
        as_designed = True

    last_10x = None
    for c in sorted(closed, key=lambda r: r["exit_at"] or "", reverse=True):
        if (c["realized_multiple"] or 0) >= TAIL_X:
            last_10x = from_iso(c["exit_at"]); break
    days_since_10x = round((now - last_10x).total_seconds() / 86400.0, 1) if last_10x else None

    n_calls = len(state.query("SELECT id FROM positions"))
    n_open = len(state.query("SELECT id FROM positions WHERE state IN ('ENTERED','SECURED','RIDING')"))
    n_watching = len(state.query("SELECT id FROM positions WHERE state='WATCHING'"))

    # Sequential bankroll reality (honest): what a FINITE bankroll actually ends at, not the
    # unconstrained per-trade sum. These are the BACKTEST SEED replay's endpoints (the
    # "b/t $3-fixed net" tiles), so read them from SEED rows only — identifiable by a
    # non-null expected_equity_usd; live engine heartbeats must not shadow them.
    start = float(state.get_system("bankroll_start_usd", "500") or 500)
    bh = state.bankroll_series()
    final_fixed = next((r["realized_equity_usd"] for r in reversed(bh)
                        if r["expected_equity_usd"] is not None
                        and r["realized_equity_usd"] is not None), start)
    final_frac = next((r["expected_equity_usd"] for r in reversed(bh)
                       if r["expected_equity_usd"] is not None), start)

    return {
        "n_positions": n, "n_calls": n_calls, "n_open": n_open, "n_watching": n_watching,
        "win_rate": round(wins / n, 4),
        "per_trade_mean": round(mean, 4),
        "per_trade_mean_ex_tail": round(ex_tail, 4),
        "best_multiple": round(best, 2),
        "top1_pnl_pct": round(100 * top1 / gross_gains, 1),
        "top3_pnl_pct": round(100 * top3 / gross_gains, 1),
        "bleed_rate": round(sum(1 for m in mults if m < 1) / n, 4),
        "total_loss_rate": round(sum(1 for m in mults if m < 0.1) / n, 4),
        "days_since_last_10x": days_since_10x,
        "hill_alpha": round(_hill_alpha(mults) or 0.0, 3),
        "realized_pnl_unconstrained": round(realized_pnl, 2),   # sum of independent $ bets (unconstrained)
        "final_fixed_usd": round(final_fixed, 2),
        "final_fractional_usd": round(final_frac, 2),
        "net_fixed_usd": round(final_fixed - start, 2),
        "net_fractional_usd": round(final_frac - start, 2),
        "bankroll_start_usd": start,
        # "as designed": realized win% and ex-tail mean sit in the power-law's expected (sub-1) regime
        "as_designed": as_designed,
        # scoped restructure: same metric set per scope, split by the live/seed rule
        **scopes,
    }


def bankroll_series(state: LiveState) -> list[dict]:
    """Three tagged series for the equity chart. SEED rows (written by the seed replay,
    identifiable by a non-null expected_equity_usd) carry the backtest fixed/fractional
    curves; LIVE rows (engine heartbeats) carry the live account balance (realized +
    open marks). Kept as separate keys so the chart never stitches seed P&L into the
    live account — that seam produced a fake vertical jump on 2026-07-03."""
    out = []
    for r in state.bankroll_series():
        is_seed = r["expected_equity_usd"] is not None
        out.append({
            "ts": r["ts"],
            "fixed": r["realized_equity_usd"] if is_seed else None,
            "fractional": r["expected_equity_usd"],
            "live": None if is_seed else r["unrealized_equity_usd"],
            "realized_pnl_cum": r["realized_pnl_cum_usd"],
        })
    return out


def equity_series(state: LiveState) -> dict:
    """The equity chart pre-split for the frontend tabs: LIVE (engine heartbeats) vs the
    BACKTEST SEED replay's fixed/fractional curves — separate arrays so the frontend can
    never stitch them (the mixed chart faked a vertical jump at the seam on 2026-07-03)."""
    live: list[list] = []
    seed_fixed: list[list] = []
    seed_frac: list[list] = []
    for r in state.bankroll_series():
        if r["expected_equity_usd"] is not None:            # SEED replay row
            if r["realized_equity_usd"] is not None:
                seed_fixed.append([r["ts"], r["realized_equity_usd"]])
            seed_frac.append([r["ts"], r["expected_equity_usd"]])
        elif r["unrealized_equity_usd"] is not None:        # LIVE heartbeat row
            live.append([r["ts"], r["unrealized_equity_usd"]])
    live_since = state.get_system("seeded_at")
    if not live_since:
        first_sig = state.query("SELECT ts FROM signals ORDER BY ts LIMIT 1")
        live_since = first_sig[0]["ts"] if first_sig else None
    return {"live": live, "seed_fixed": seed_fixed, "seed_frac": seed_frac,
            "live_since": live_since}


def open_positions(state: LiveState) -> list[dict]:
    lm = _live_mints(state)
    now = _now(state)
    rows = state.query(
        "SELECT mint,ticker,state,signal_price,signal_at,dip_deadline,entry_price,"
        "current_price,current_multiple,realized_pnl_usd,stake_usd,next_rung_mult,next_rung_price,"
        "secured,n_tp,remaining_frac,stop_price,peak_price,low_price,opened_at "
        "FROM positions WHERE state IN ('WATCHING','ENTERED','SECURED','RIDING') "
        "ORDER BY current_multiple DESC")
    cmap = _controller_map(state)     # L5: attach controller defensively (never 500 on a stale DB)
    for r in rows:
        r["controller"] = cmap.get(r["mint"], "algo")
        r["source"] = "live" if _mint_is_live(r["mint"], lm) else "seed"
        opened = _parse_ts(r["opened_at"]) or _parse_ts(r["signal_at"])
        r["age_h"] = round((now - opened).total_seconds() / 3600.0, 2) if opened else None
        r["dip_progress_pct"] = None       # WATCHING: % of the -50% dip already travelled
        r["dip_deadline_h_left"] = None
        r["dist_to_next_rung_pct"] = None  # entered: % rise still needed to the next rung
        r["peak_multiple"] = None
        r["dist_to_stop_pct"] = None       # unsecured only: % drop from here to the stop
        r["dip_low_pct"] = None            # WATERMARK: deepest dip reached, % toward the trigger
        r["above_anchor_pct"] = None       # momentum visibility: % ABOVE the call anchor right now
        r["pct_from_call"] = None          # plain %: price vs the call anchor (negative = down)
        r["low_pct_from_call"] = None      # plain %: deepest low vs the call anchor
        if r["state"] == "WATCHING":
            sig, cur, low = r["signal_price"], r["current_price"], r["low_price"]
            if sig and cur is not None:
                # trigger = 0.5*sig (the locked -50% dip); 100% = trigger hit
                r["dip_progress_pct"] = round(min(100.0, max(0.0, 100.0 * (sig - cur) / (0.5 * sig))), 1)
                r["pct_from_call"] = round(100.0 * (cur / sig - 1.0), 1)
                if cur > sig:
                    r["above_anchor_pct"] = round(100.0 * (cur / sig - 1.0), 1)
            if sig and low is not None:
                r["dip_low_pct"] = round(min(100.0, max(0.0, 100.0 * (sig - low) / (0.5 * sig))), 1)
                r["low_pct_from_call"] = round(100.0 * (low / sig - 1.0), 1)
            ddl = _parse_ts(r["dip_deadline"])
            if ddl:
                r["dip_deadline_h_left"] = round(max(0.0, (ddl - now).total_seconds() / 3600.0), 2)
        else:
            entry, cur = r["entry_price"], r["current_price"]
            if entry and r["peak_price"]:
                r["peak_multiple"] = round(r["peak_price"] / entry, 3)
            if cur and r["next_rung_price"]:
                r["dist_to_next_rung_pct"] = round(100.0 * (r["next_rung_price"] - cur) / cur, 1)
            if cur and r["stop_price"] and not r["secured"]:
                r["dist_to_stop_pct"] = round(100.0 * (cur - r["stop_price"]) / cur, 1)
    return rows


def trade_history(state: LiveState, scope: str = "live", limit: int = 100) -> list[dict]:
    """Newest-first closed history: realized trades (closed_trades) merged with EXPIRED
    watchers (never entered — the honest denominator). scope: "live" | "seed" | "all"."""
    lm = _live_mints(state)
    rows: list[dict] = []
    for c in state.closed_trades():
        rows.append({
            "closed_at": c["exit_at"], "ticker": c["ticker"] or c["mint"][:4], "mint": c["mint"],
            "kind": "trade", "close_reason": c["close_reason"],
            "entry_price": c["entry_price"],
            "realized_multiple": c["realized_multiple"],
            "pnl_usd": c["pnl_usd"] or 0.0,
            "held_hours": c["held_hours"], "n_tp": c["n_tp"],
            "was_stopped": bool(c["was_stopped"]), "was_secured": bool(c["was_secured"]),
            "source": "live" if _mint_is_live(c["mint"], lm) else "seed",
        })
    for p in state.query("SELECT mint,ticker,closed_at,updated_at FROM positions WHERE state='EXPIRED'"):
        rows.append({
            "closed_at": p["closed_at"] or p["updated_at"],
            "ticker": p["ticker"] or p["mint"][:4], "mint": p["mint"],
            "kind": "expired", "close_reason": "no dip within 48h",
            "entry_price": None, "realized_multiple": None, "pnl_usd": 0.0,
            "held_hours": None, "n_tp": 0, "was_stopped": False, "was_secured": False,
            "source": "live" if _mint_is_live(p["mint"], lm) else "seed",
        })
    if scope in ("live", "seed"):
        rows = [r for r in rows if r["source"] == scope]
    rows.sort(key=lambda r: r["closed_at"] or "", reverse=True)
    return rows[:limit]


# position_events.event_type -> the STREAM's human action label
# NB: production books a DIRECT BUY as event_type='ENTER' (algo rides it) — there is no durable
# 'MANUAL_BUY' event, so no mapping for it here (audit #27).
_STREAM_ACTIONS = {
    "SIGNAL": "CALL", "ENTER": "BUY", "TP": "TAKE PROFIT", "RIDE_SELL": "RIDE SELL",
    "STOP_OUT": "CUT", "FINALIZE": "FINAL SELL", "EXPIRE": "EXPIRED",
    "MANUAL_SELL": "MANUAL SELL",
}


def exec_stream(state: LiveState, scope: str = "live", limit: int = 120) -> list[dict]:
    """Newest-first raw execution feed: EVERY order/event the machine took, one row per
    position_events row (the append-only source of truth). Per-leg realized P&L for
    sells = proceeds − cost basis of the fraction sold (frac is of ORIGINAL notional,
    so cost = frac × stake). FDV is enriched at the API layer (price × token supply)."""
    lm = _live_mints(state)
    try:
        evs = state.query(
            "SELECT e.id, e.mint, e.ts, e.event_type, e.price, e.rung_mult, e.frac, "
            "       e.proceeds_usd, e.remaining_frac, e.note, p.ticker, p.stake_usd "
            "FROM position_events e LEFT JOIN positions p ON p.mint = e.mint "
            # audit #27: exclude MARK and the durable *_SUBMITTED intent markers (written in live mode)
            # — else every executed leg shows twice: the intent row + the confirmed row.
            "WHERE e.event_type != 'MARK' AND e.event_type NOT LIKE '%\\_SUBMITTED' ESCAPE '\\' "
            "ORDER BY e.id DESC LIMIT 5000")  # id order ==
        # insertion order == ts order here, and the PK serves it without a scan-sort
    except sqlite3.OperationalError:
        return []
    rows: list[dict] = []
    for e in evs:
        source = "live" if _mint_is_live(e["mint"], lm) else "seed"
        if scope in ("live", "seed") and source != scope:
            continue
        et = e["event_type"]
        stake = e["stake_usd"] or 0.0
        value = pnl = None
        if et == "ENTER":
            value = stake or None
        elif et in ("TP", "RIDE_SELL", "STOP_OUT", "FINALIZE", "MANUAL_SELL"):
            value = e["proceeds_usd"]
            if value is not None and e["frac"] is not None and stake:
                pnl = value - e["frac"] * stake
        rows.append({
            "id": e["id"], "ts": e["ts"], "mint": e["mint"],
            "ticker": e["ticker"] or e["mint"][:4],
            "action": _STREAM_ACTIONS.get(et, et), "event_type": et,
            "price": e["price"], "rung_mult": e["rung_mult"], "frac": e["frac"],
            "remaining_frac": e["remaining_frac"],
            "value_usd": round(value, 2) if value is not None else None,
            "pnl_usd": round(pnl, 2) if pnl is not None else None,
            "note": e["note"], "source": source,
        })
        if len(rows) >= limit:
            break
    return rows


def lab_config_detail(state: LiveState, config_id: str) -> Optional[dict]:
    """One challenger's full story: its knobs, every open rider, every closed leg."""
    params = None
    try:
        from memebot.live.shadow import CHALLENGERS, load_custom_challengers
        for c in tuple(CHALLENGERS) + load_custom_challengers(state):
            if c.id == config_id:
                params = {"id": c.id, "label": c.label, "family": c.family,
                          "dip": c.dip, "sl": c.sl, "ftp": c.ftp, "fsell": c.fsell,
                          "reentry": c.reentry, "entry_mode": c.entry_mode,
                          "exit_policy": c.exit_policy, "heat_min": c.heat_min}
                break
    except Exception:
        pass
    riders: list[dict] = []
    trades: list[dict] = []
    try:
        px = {r["mint"]: r["current_price"] for r in
              state.query("SELECT mint, current_price FROM positions")}
        tick = {r["mint"]: r["ticker"] for r in state.query("SELECT mint, ticker FROM positions")}
        for r in state.query("SELECT mint, state, snapshot_json, updated_at FROM shadow_riders "
                             "WHERE config_id = ? ORDER BY updated_at DESC", (config_id,)):
            snap = {}
            try:
                snap = json.loads(r["snapshot_json"] or "{}")
            except Exception:
                pass
            mult = None
            p = px.get(r["mint"])
            leg = snap.get("cur") or snap.get("trail")
            if leg and leg.get("entry") and p:
                mult = round(((leg.get("pr") or 0.0) + (leg.get("rem") or 0.0) * p)
                             / leg["entry"], 3)
            riders.append({"mint": r["mint"], "ticker": snap.get("ticker") or tick.get(r["mint"])
                           or r["mint"][:4], "status": r["state"], "mark_multiple": mult,
                           "n_legs_done": len(snap.get("legs") or []),
                           "updated_at": r["updated_at"]})
        trades = state.query("SELECT mint, ticker, entered_at, closed_at, realized_multiple, "
                             "close_reason FROM shadow_trades WHERE config_id = ? "
                             "ORDER BY closed_at DESC LIMIT 200", (config_id,))
    except sqlite3.OperationalError:
        pass
    if params is None and not riders and not trades:
        return None
    return {"config_id": config_id, "params": params, "riders": riders, "trades": trades}


def daily_pnl(state: LiveState) -> list[dict]:
    """Realized P&L of LIVE trades per UTC day (for a small bar chart). Empty list is fine."""
    lm = _live_mints(state)
    days: dict[str, dict] = {}
    for c in state.closed_trades():
        if not _mint_is_live(c["mint"], lm):
            continue
        d = (c["exit_at"] or "")[:10]
        rec = days.setdefault(d, {"date": d, "realized_pnl": 0.0, "n_closed": 0})
        rec["realized_pnl"] += c["pnl_usd"] or 0.0
        rec["n_closed"] += 1
    return [{"date": d, "realized_pnl": round(r["realized_pnl"], 2), "n_closed": r["n_closed"]}
            for d, r in sorted(days.items())]


def signal_flow(state: LiveState) -> dict:
    """Is the pipeline moving? Call cadence + how the live funnel resolves."""
    now = _now(state)
    lm = _live_mints(state)

    def _first_calls_since(dt: datetime) -> int:
        rows = state.query("SELECT COUNT(*) AS n FROM signals WHERE is_first_call=1 AND ts >= ?",
                           (dt.isoformat(),))
        return rows[0]["n"] if rows else 0

    pos = state.query("SELECT mint, state, entry_at FROM positions")
    live = [p for p in pos if _mint_is_live(p["mint"], lm)]
    entered = sum(1 for p in live if p["entry_at"])
    watching = sum(1 for p in live if p["state"] == "WATCHING")
    expired = sum(1 for p in live if p["state"] == "EXPIRED")
    resolved = len(live) - watching        # past the WATCHING stage: entered, expired or closed
    return {
        "calls_24h": _first_calls_since(now - timedelta(hours=24)),
        "calls_7d": _first_calls_since(now - timedelta(days=7)),
        "entry_rate_pct": round(100.0 * entered / len(live), 1) if live else None,
        "watching_now": watching,
        "expired_no_dip_pct": round(100.0 * expired / resolved, 1) if resolved else None,
    }


# terminal states shared with shadow_riders snapshots (mirrors positions.state)
_TERMINAL_STATES = ("EXITED", "STOPPED", "EXPIRED")


def lab(state: LiveState) -> dict:
    """Strategy-lab read: shadow configs + research runs written by the adaptive engine.

    Those tables are created by a PARALLEL workstream — read them defensively (a missing
    table returns the empty-but-shaped dict, never raises)."""
    out: dict = {"champion": state.get_system("champion_config_id") or "C1",
                 "configs": {}, "last_research": None, "research_running": False}
    # config_id -> {label, family}: the frontend stops hardcoding labels. Defensive import —
    # shadow.py belongs to a parallel workstream; a broken import must never 500 the dashboard.
    try:
        from memebot.live.shadow import CHALLENGERS, load_custom_challengers
        out["meta"] = {c.id: {"label": c.label, "family": getattr(c, "family", "core")}
                       for c in CHALLENGERS}
        for c in load_custom_challengers(state):
            out["meta"][c.id] = {"label": c.label, "family": "custom"}
    except Exception:
        out["meta"] = {}
    now = _now(state)

    trades: list[dict] = []
    try:
        trades = state.query("SELECT config_id, realized_multiple FROM shadow_trades "
                             "WHERE realized_multiple IS NOT NULL")
    except sqlite3.OperationalError:
        pass
    open_counts: dict[str, int] = {}
    try:
        for r in state.query("SELECT config_id, COUNT(*) AS n FROM shadow_riders "
                             "WHERE state NOT IN (?,?,?) GROUP BY config_id", _TERMINAL_STATES):
            open_counts[r["config_id"]] = r["n"]
    except sqlite3.OperationalError:
        pass

    # MARK-TO-MARKET of open riders, so each config's TOTAL reconciles with the account
    # balance (the user caught C1 "in loss" while the balance was "in profit" — the lab
    # showed closed legs only while an open winner sat in the balance's unrealized side).
    # Value each open rider at the mint's latest price (positions.current_price — the
    # engine keeps it fresh for any mint the feed still tracks): completed-but-unflushed
    # legs at their final multiple, the in-flight leg at (pr + rem*price)/entry.
    open_pnl: dict[str, float] = {}
    try:
        px = {r["mint"]: r["current_price"] for r in
              state.query("SELECT mint, current_price FROM positions")}
        for r in state.query("SELECT config_id, mint, snapshot_json FROM shadow_riders "
                             "WHERE state NOT IN (?,?,?)", _TERMINAL_STATES):
            try:
                snap = json.loads(r["snapshot_json"] or "{}")
                # legs are DICTS ({"multiple", ...}); only UNFLUSHED ones count here —
                # flushed legs already sit in shadow_trades (the realized column).
                unflushed = (snap.get("legs") or [])[int(snap.get("flushed") or 0):]
                pnl = sum(3.0 * (leg["multiple"] - 1.0) for leg in unflushed
                          if isinstance(leg, dict) and leg.get("multiple") is not None)
                p = px.get(r["mint"])
                for leg in (snap.get("cur"), snap.get("trail")):
                    if leg and leg.get("entry") and p:
                        mult = ((leg.get("pr") or 0.0) + (leg.get("rem") or 0.0) * p) / leg["entry"]
                        pnl += 3.0 * (mult - 1.0)
                open_pnl[r["config_id"]] = open_pnl.get(r["config_id"], 0.0) + pnl
            except Exception:
                continue                        # one unparseable snapshot never breaks the lab
    except sqlite3.OperationalError:
        pass

    by_cfg: dict[str, list[float]] = {}
    for t in trades:
        by_cfg.setdefault(t["config_id"], []).append(t["realized_multiple"])
    for cfg, ms in by_cfg.items():
        n = len(ms)
        srt = sorted(ms, reverse=True)
        mean = sum(ms) / n
        realized = round(3.0 * sum(m - 1.0 for m in ms), 2)
        opn = round(open_pnl.get(cfg, 0.0), 2)
        out["configs"][cfg] = {
            "n_trades": n,
            "n_open": open_counts.get(cfg, 0),
            "mean": round(mean, 4),
            "drop_top1_mean": round((sum(srt[1:]) / (n - 1)) if n > 1 else mean, 4),
            "win_rate": round(sum(1 for m in ms if m > 1) / n, 4),
            "sum_pnl_at_3usd": realized,
            "open_pnl_at_3usd": opn,
            "total_pnl_at_3usd": round(realized + opn, 2),
        }
    for cfg, n_open in open_counts.items():     # riders but nothing closed yet
        opn = round(open_pnl.get(cfg, 0.0), 2)
        out["configs"].setdefault(cfg, {"n_trades": 0, "n_open": n_open, "mean": None,
                                        "drop_top1_mean": None, "win_rate": None,
                                        "sum_pnl_at_3usd": 0.0,
                                        "open_pnl_at_3usd": opn,
                                        "total_pnl_at_3usd": opn})
    # A just-added custom strategy has no riders until the NEXT call — give it a zero
    # row anyway so it appears (and can be inspected/deleted) the moment it's created.
    for cfg, m in out["meta"].items():
        if m.get("family") == "custom":
            out["configs"].setdefault(cfg, {"n_trades": 0, "n_open": 0, "mean": None,
                                            "drop_top1_mean": None, "win_rate": None,
                                            "sum_pnl_at_3usd": 0.0, "open_pnl_at_3usd": 0.0,
                                            "total_pnl_at_3usd": 0.0})

    try:
        runs = state.query("SELECT ts, status, verdict_json FROM research_runs ORDER BY ts DESC LIMIT 10")
        if runs and runs[0]["verdict_json"]:
            # newest row INCLUDING a live 'running' one — the UI shows its progress fields
            # (phase / priced / total / started_at) — with the row's status merged in.
            try:
                lr = json.loads(runs[0]["verdict_json"])
                if isinstance(lr, dict):
                    lr["status"] = runs[0]["status"]
                    out["last_research"] = lr
            except (TypeError, ValueError):
                pass
        for r in runs:
            if r["status"] == "running":
                # prefer the run's own started_at (in verdict_json); fall back to row ts
                started = None
                try:
                    v = json.loads(r["verdict_json"] or "{}")
                    if isinstance(v, dict):
                        started = _parse_ts(v.get("started_at"))
                except (TypeError, ValueError):
                    pass
                started = started or _parse_ts(r["ts"])
                if started and (now - started) < timedelta(hours=2):
                    out["research_running"] = True
                    break
    except sqlite3.OperationalError:
        pass
    return out


def signal_feed(state: LiveState, limit: int = 60) -> list[dict]:
    return state.query(
        "SELECT ts,ticker,mint,side,is_first_call,accepted,reject_reason FROM signals "
        "ORDER BY ts DESC LIMIT ?", (limit,))


# --------------------------------------------------------------------------- #
# MANUAL layer reads (the human-discretion desk + manual-vs-algo attribution)
def _order_view(o: dict, price_by_mint: dict) -> dict:
    """Shape one order row for the UI, with a live-progress hint toward its trigger."""
    px = price_by_mint.get(o["mint"])
    progress = None
    tt, tv = o["trigger_type"], o["trigger_value"]
    if px and tv:
        if tt == "price_at_or_below" and px > 0:
            progress = round(min(100.0, max(0.0, 100.0 * (px - tv) / px)), 1)   # how far above the limit
        elif tt == "price_at_or_above" and tv > 0:
            progress = round(min(100.0, max(0.0, 100.0 * (px / tv))), 1)
    return {
        "id": o["id"], "mint": o["mint"], "ticker": o["ticker"] or o["mint"][:4],
        "kind": o["kind"], "side": o["side"], "trigger_type": tt, "trigger_value": tv,
        "size_kind": o["size_kind"], "size_value": o["size_value"], "status": o["status"],
        "hwm": o["hwm"], "note": o["note"], "created_at": o["created_at"],
        "expires_at": o["expires_at"], "current_price": px, "progress_pct": progress,
    }


def manual_desk(state: LiveState) -> dict:
    """The human-discretion desk: open orders (with live progress), the watchlist, manual positions,
    and the caps — everything the MANUAL DESK panel + trade tickets render. Defensive: a DB created
    before the manual layer returns the empty-but-shaped dict, never raises."""
    out = {"orders": [], "watchlist": [], "positions": [], "n_open_orders": 0,
           "manual_exposure_usd": 0.0,
           "caps": {"manual_cap_usd": float(state.get_system("manual_cap_usd") or 0.0),
                    "manual_trade_hard_cap_usd": float(state.get_system("manual_trade_hard_cap_usd") or 0.0)}}
    try:
        px = {r["mint"]: r["current_price"] for r in
              state.query("SELECT mint, current_price FROM positions")}
        orders = state.open_orders()
        out["orders"] = [_order_view(o, px) for o in orders]
        out["n_open_orders"] = len(orders)
        out["watchlist"] = state.watchlist()
        rows = state.query(
            "SELECT mint,ticker,state,entry_price,current_price,current_multiple,stake_usd,"
            "tokens_qty,remaining_frac,peak_price,opened_at,realized_pnl_usd "
            "FROM positions WHERE controller='manual' AND state IN ('ENTERED','SECURED','RIDING') "
            "ORDER BY opened_at DESC")
        for r in rows:
            entry, peak = r["entry_price"], r["peak_price"]
            r["peak_multiple"] = round(peak / entry, 3) if (entry and peak) else None
            r["n_open_orders"] = sum(1 for o in orders if o["mint"] == r["mint"])
        out["positions"] = rows
        out["manual_exposure_usd"] = round(sum(
            (r["stake_usd"] or 0.0) * (r["remaining_frac"] if r["remaining_frac"] is not None else 1.0)
            for r in rows), 2)
    except sqlite3.OperationalError:
        pass
    return out


def attribution(state: LiveState) -> dict:
    """Manual-vs-algo P&L split over LIVE closed trades — measures the human's discretionary edge
    honestly against the machine (like the shadow lab measures challengers). Controller is read
    from the still-present position row (closed_trades has no controller column)."""
    lm = _live_mints(state)
    ctrl = _controller_map(state)            # L5: guarded (empty -> all 'algo', never 500)
    out = {"manual": _blank_attr(), "algo": _blank_attr()}
    for c in state.closed_trades():
        if not _mint_is_live(c["mint"], lm):
            continue
        bucket = "manual" if ctrl.get(c["mint"]) == "manual" else "algo"
        b = out[bucket]
        b["_mults"].append(c["realized_multiple"] or 0.0)
        b["n"] += 1
        b["realized_pnl"] += c["pnl_usd"] or 0.0
    # open manual/algo marks (unrealized) so the split reflects live exposure too
    for p in state.query("SELECT mint,stake_usd,current_multiple FROM positions "
                         "WHERE state IN ('ENTERED','SECURED','RIDING')"):
        if not _mint_is_live(p["mint"], lm):
            continue
        bucket = "manual" if ctrl.get(p["mint"]) == "manual" else "algo"
        if p["current_multiple"] is not None and p["stake_usd"]:
            out[bucket]["unrealized_pnl"] += p["stake_usd"] * (p["current_multiple"] - 1.0)
            out[bucket]["n_open"] += 1
    for b in out.values():
        ms = b.pop("_mults")
        n = len(ms)
        b["win_rate"] = round(sum(1 for m in ms if m > 1) / n, 4) if n else None
        b["mean"] = round(sum(ms) / n, 4) if n else None
        b["best"] = round(max(ms), 2) if n else None
        b["realized_pnl"] = round(b["realized_pnl"], 2)
        b["unrealized_pnl"] = round(b["unrealized_pnl"], 2)
    return out


def _blank_attr() -> dict:
    return {"_mults": [], "n": 0, "n_open": 0, "realized_pnl": 0.0, "unrealized_pnl": 0.0}


def _sysfloat(state: LiveState, key: str) -> Optional[float]:
    raw = state.get_system(key)
    try:
        return float(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def meta(state: LiveState) -> dict:
    return {
        "mode": state.get_system("mode", "paper"),
        "kill_switch": state.get_system("kill_switch", "off"),
        "bankroll_start_usd": float(state.get_system("bankroll_start_usd", "500") or 500),
        # the REAL burner wallet (live book only; written by the engine's wallet refresh) — the
        # honest "what do I actually own" line the account panel shows
        "wallet_sol": _sysfloat(state, "wallet_sol"),
        "wallet_usd": _sysfloat(state, "wallet_usd"),
        "wallet_at": state.get_system("wallet_at"),
        "seeded_at": state.get_system("seeded_at"),
        "seed_source": state.get_system("seed_source"),
        "exec_pending": int(state.get_system("exec_pending") or 0),   # live swaps in flight
        "n_open_orders": _count_open_orders(state),                   # manual orders resting (header badge)
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "loss_floor": LOSS_FLOOR, "tail_x": TAIL_X,
        # config #1 rungs for markers/annotations
        "rungs": [3, 6, 12, 24, 48], "dip": 0.5, "stop_pct": -30,
    }


def _count_open_orders(state: LiveState) -> int:
    try:
        rows = state.query("SELECT COUNT(*) AS n FROM orders WHERE status IN ('open','submitted')")
        return rows[0]["n"] if rows else 0
    except sqlite3.OperationalError:
        return 0


def token_detail(state: LiveState, mint: str) -> Optional[dict]:
    """Per-token drill-down: the position, its lifecycle events, and config #1's rung levels."""
    pos = state.get_position(mint)
    if not pos:
        return None
    events = state.events_for(mint)
    entry = pos.get("entry_price")
    rungs = []
    if entry:
        rungs = [
            {"label": "entry", "mult": 1.0, "price": entry},
            {"label": "−30% stop", "mult": 0.70, "price": 0.70 * entry},
            {"label": "3× secure", "mult": 3.0, "price": 3.0 * entry},
            {"label": "6×", "mult": 6.0, "price": 6.0 * entry},
            {"label": "12×", "mult": 12.0, "price": 12.0 * entry},
            {"label": "24×", "mult": 24.0, "price": 24.0 * entry},
            {"label": "48×", "mult": 48.0, "price": 48.0 * entry},
        ]
    return {"position": pos, "events": events, "rungs": rungs}


def ccdf(state: LiveState, points: Optional[list[dict]] = None) -> dict:
    """Complementary CDF of the multiples on log-log axes + the Hill alpha (the tail's shape)."""
    pts = points if points is not None else hero_points(state)
    mults = sorted((p["multiple"] for p in pts if p["multiple"] and p["multiple"] > 0), reverse=True)
    n = len(mults)
    curve = [{"x": m, "p": (i + 1) / n} for i, m in enumerate(mults)]   # P(X >= m)
    return {"curve": curve, "alpha": _hill_alpha(mults), "n": n}


def snapshot(state: LiveState) -> dict:
    points = hero_points(state)
    history_all = trade_history(state, "all", limit=1_000_000)
    return {
        "meta": meta(state),
        "account": account(state),        # the headline balance block (live activity only)
        "hero": _pareto(points),          # points + rank + cumulative Pareto %, ready for the hero
        "stats": stats(state, points),    # legacy top-level keys + live/seed/all scopes
        "ccdf": ccdf(state, points),      # log-log survival curve + Hill alpha
        # (the old "bankroll" key was dropped — the frontend reads only "equity"; keeping it
        #  meant a full bankroll_history scan + per-row dict on every ~2s snapshot push)
        "equity": equity_series(state),   # LIVE vs SEED tabs — never mixed on one curve
        "history": {                      # newest-first live slice; /api/history pages further
            "rows": [r for r in history_all if r["source"] == "live"][:100],
            "live_count": sum(1 for r in history_all if r["source"] == "live"),
            "seed_count": sum(1 for r in history_all if r["source"] == "seed"),
        },
        "daily_pnl": daily_pnl(state),
        "signal_flow": signal_flow(state),
        "lab": lab(state),
        "positions": open_positions(state),
        "signals": signal_feed(state),
        "alerts": state.recent_alerts(50),
        # audit #26: manual_desk()/attribution() are NOT in the ~2s snapshot push — no frontend
        # component consumes them post-rework (all manual state rides open_positions + the orders
        # routes), so re-materializing them on every push was dead work. The functions remain for a
        # future on-demand REST endpoint.
    }
