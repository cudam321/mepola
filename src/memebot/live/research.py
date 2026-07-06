"""Automated re-measurement — honest adaptation, parts (b) and (c).

Runs INSIDE the engine container (launched from run.py via asyncio.to_thread, weekly or on
dashboard demand) and re-asks the research question with the exact discipline that killed
six overfit artifacts: refresh the corpus, re-price every first-call to today, run the FULL
stage37 5-param grid with the golden `sim`, split 70/30 chronologically, and gate on OOS:

    bootstrap-CI-lower(mean) > 1  AND  drop-top-3 mean > 1  AND
    fixed-f log-growth(f=2%) > 0  AND  a $500 single-pass chronological bankroll grows.

Never point EV, never max-over-policies (that printed a false GO — see
RESEARCH.md). The expected, honest outcome of most runs is "no change":
config #1 stays a tail bet and nothing clears. A naive re-optimizer chasing recent
performance would have abandoned #1 during its normal bleed right before the 197x tail.

Output is a RECOMMENDATION, never an action: the verdict is written to `research_runs` and
surfaced on the dashboard; promotion happens only when a HUMAN flips
`system_state.champion_config_id`. This module never modifies the live strategy.

Helper formulas (mean_ci / drop_top / fixed_f_growth / single_pass_bankroll) are replicated
from scripts/stage14_untruncated.py exactly (GAS=0.6, seed=0 bootstrap); `sim` is copied
verbatim from tests/sim_oracle.py. Nothing is imported from scripts/ (import-time side
effects) — these references are LOCKED, so drift means someone changed the canon: update
in lockstep or not at all.
"""

from __future__ import annotations

import asyncio
import itertools
import os
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import numpy as np

from memebot.ingest.telegram_mcp import first_call_per_mint, load_corpus_json
from memebot.live.state import LiveState, utcnow
from memebot.models import PriceSeries

log = logging.getLogger("memebot.live.research")

W48 = 48 * 3600
GAS = 0.6                  # round-trip fixed solana cost per trade, USD (stage14)
HOLD_DAYS = 45             # stage14 horizon: min(t0+45d, now)
MIN_FETCH_H = 12           # minute-resolution window near entry (stage14)
CHANNEL = os.environ.get("MEMEBOT_CHANNEL", "@your_channel")
CORPUS_PULL_LIMIT = 6000

# The FULL stage37 5-param grid: dip x sl x ftp x fsell x reentry (144 configs).
GRID = list(itertools.product([0, 0.3, 0.5], [0, 0.3, 0.5, 0.7], [1.5, 2.0, 3.0],
                              [0.33, 0.5], [3.0, None]))
CHAMPION_CFG = (0.5, 0.7, 3.0, 0.33, None)     # config #1 (it is a member of GRID)


# --------------------------------------------------------------------------- #
# The golden reference sim — copied VERBATIM from tests/sim_oracle.py (itself a
# byte-for-byte copy of scripts/stage37_grid.py::sim). DO NOT EDIT THE BODY.
# --------------------------------------------------------------------------- #
def sim(H, L, C, T, sig, dip=0.5, sl=0.7, ftp=3.0, fsell=0.33, reentry=None):
    n = len(H)
    if dip == 0:
        start = 0; entry = sig * 1.01
    else:
        start = None
        for j in range(n):
            if T[j] - T[0] > W48: break
            if L[j] <= (1 - dip) * sig: start = j; entry = (1 - dip) * sig * 1.01; break
        if start is None: return None
    legs = []; i = start
    while i < n and len(legs) < 8:
        rem = 1.0; pr = 0.0; ntp = 0; lvl = ftp; sec = False; stp = False; expx = C[-1]; eidx = n - 1
        for j in range(i, n):
            if rem <= 1e-9: eidx = j; break
            if (not sec) and sl > 0 and L[j] <= sl * entry:
                pr += rem * sl * entry * 0.95; rem = 0; stp = True; expx = sl * entry; eidx = j; break
            while rem > 1e-9 and H[j] >= lvl * entry:
                s = min(fsell if ntp == 0 else 0.25 * rem, rem)
                pr += s * lvl * entry * 0.985; rem -= s; ntp += 1
                if ntp == 1: sec = True
                lvl = lvl * 2 if ntp < 5 else lvl * 3
        if rem > 1e-9: pr += rem * C[-1]
        legs.append(pr / entry)
        if not stp or reentry is None: break
        tgt = reentry * expx; k = eidx + 1
        while k < n and H[k] < tgt: k += 1
        if k >= n: break
        entry = tgt * 1.01; i = k
    return legs


# --------------------------------------------------------------------------- #
# Gate metrics — replicated from scripts/stage14_untruncated.py EXACTLY.
# --------------------------------------------------------------------------- #
def mean_ci(mults, n=5000, seed=0):
    a = np.asarray(mults, dtype=float)
    if len(a) < 2:
        return float(a.mean()), float("nan"), float("nan")
    rng = np.random.default_rng(seed)
    bs = a[rng.integers(0, len(a), size=(n, len(a)))].mean(axis=1)
    return float(a.mean()), float(np.percentile(bs, 2.5)), float(np.percentile(bs, 97.5))


def fixed_f_growth(mults, f=0.02):
    a = np.asarray(mults, dtype=float)
    return float(np.mean(np.log(np.maximum(1 + f * (a - 1), 1e-9))))


def drop_top(mults, k):
    a = np.sort(np.asarray(mults, dtype=float))
    return float(a[:-k].mean()) if len(a) > k else float("nan")


def single_pass_bankroll(mults, times, f=0.02, cap=float("inf"), start=500.0):
    """Honest bankroll: each token traded ONCE, in chronological order, fractional sizing,
    liquidity-capped multiple, fixed gas. No resampling."""
    order = np.argsort(times)
    B = start
    for j in order:
        if B <= 0:
            break
        stake = f * B
        m = min(mults[j], cap)
        B = B - stake - GAS + stake * m
    return float(B)


def gate_pass(mults, times, *, f=0.02, start=500.0) -> tuple[bool, dict]:
    """THE measurement bar (the discipline that killed every false GO). A config 'clears'
    only if OOS: CI-lower(mean) > 1 AND drop-top-3 > 1 AND f=2% log-growth > 0 AND a $500
    single-pass chronological bankroll grows. NaNs (too few observations) fail closed."""
    a = [float(m) for m in mults]
    if len(a) < 2:
        return False, {"n": len(a), "mean": float("nan"), "ci_lo": float("nan"),
                       "ci_hi": float("nan"), "drop3": float("nan"), "f2_logG": float("nan"),
                       "bank_500": float("nan"), "clears": False}
    m, lo, hi = mean_ci(a)
    d3 = drop_top(a, 3)
    g2 = fixed_f_growth(a, f)
    bank = single_pass_bankroll(a, np.asarray(times, dtype=float), f=f, start=start)
    ok = bool(lo > 1.0 and d3 > 1.0 and g2 > 0.0 and bank > start)
    return ok, {"n": len(a), "mean": m, "ci_lo": lo, "ci_hi": hi, "drop3": d3,
                "f2_logG": g2, "bank_500": bank, "clears": ok}


# --------------------------------------------------------------------------- #
# Corpus + pricing
# --------------------------------------------------------------------------- #
def _refresh_corpus(corpus_path: Path, channel: str = CHANNEL,
                    limit: int = CORPUS_PULL_LIMIT) -> bool:
    """Pull the channel's recent history via telethon (read-only; same credential
    resolution as listener.py). Returns True on success; False -> caller falls back to
    the existing corpus file (status='stale_corpus')."""
    # EVENT-LOOP NOTE (do not remove): this function runs inside asyncio.to_thread's worker
    # thread (run.py). telethon's sync wrapper resolves its loop via helpers.get_running_loop()
    # -> asyncio.get_event_loop_policy().get_event_loop(), which RAISES ("There is no current
    # event loop in thread 'asyncio_N'") in any non-main thread that never called
    # set_event_loop — verified empirically on telethon 1.42 / py3.13. So this thread gets its
    # OWN loop, explicitly, and tears it down in `finally` (to_thread's worker threads are
    # pooled and REUSED; a leftover closed loop would poison the next task on this thread).
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        from telethon.sessions import StringSession
        from telethon.sync import TelegramClient

        from memebot.live.listener import _resolve_credentials

        api_id, api_hash, session = _resolve_credentials()
        client = TelegramClient(StringSession(session), api_id, api_hash)
        client.connect()
        try:
            if not client.is_user_authorized():
                return False
            ent = client.get_entity(channel)
            title = getattr(ent, "title", channel)
            messages: list[dict] = []
            for m in client.iter_messages(ent, limit=limit):
                text = (m.message or "").strip()
                if not text:
                    continue
                ts = m.date.astimezone(timezone.utc) if m.date else None
                messages.append({"id": m.id, "date": int(ts.timestamp()) if ts else 0,
                                 "text": text})
        finally:
            # always disconnect BEFORE the loop teardown below: closing a loop that still
            # owns a connected client makes telethon's background tasks spew
            # "Event loop is closed" (observed empirically) — even on the error path.
            client.disconnect()
        messages.reverse()      # oldest first, same shape as scripts/pull_channel_history.py
        corpus_path.parent.mkdir(parents=True, exist_ok=True)
        corpus_path.write_text(json.dumps({"channel": channel, "title": title,
                                           "messages": messages}))
        log.info("research: corpus refreshed (%d messages) -> %s", len(messages), corpus_path)
        return True
    except Exception:
        log.exception("research: corpus refresh failed; will fall back to the existing file")
        return False
    finally:
        asyncio.set_event_loop(None)
        loop.close()


def _series_to_now(client, mint: str, t0: datetime, now: datetime) -> PriceSeries:
    """stage14's mixed-resolution recipe, replicated locally (do NOT import scripts/):
    minute candles for the first 12h + coarser candles out to min(t0+45d, now)."""
    end = min(t0 + timedelta(days=HOLD_DAYS), now)
    mn = client.get_price_series(mint, t0 - timedelta(minutes=5), t0 + timedelta(hours=MIN_FETCH_H))
    rest_start = t0 + timedelta(hours=MIN_FETCH_H)
    rest = (client.get_price_series(mint, rest_start, end)
            if end > rest_start else PriceSeries(mint, None, "hour", 1, []))
    boundary = mn.candles[-1].ts if mn.candles else t0
    candles = list(mn.candles) + [c for c in rest.candles if c.ts > boundary]
    candles.sort(key=lambda c: c.ts)
    return PriceSeries(mint=mint, pool=None, timeframe="mixed", aggregate=1, candles=candles)


def _is_cached(cache_dir: Path, mint: str, t0: datetime, now: datetime) -> bool:
    """True if both fetch windows of `_series_to_now` are already on disk (CachedPriceClient
    keys by (mint, start, end) — replicate its key format)."""
    end = min(t0 + timedelta(days=HOLD_DAYS), now)
    s1, e1 = t0 - timedelta(minutes=5), t0 + timedelta(hours=MIN_FETCH_H)
    keys = [f"{mint}_{int(s1.timestamp())}_{int(e1.timestamp())}.json"]
    rest_start = t0 + timedelta(hours=MIN_FETCH_H)
    if end > rest_start:
        keys.append(f"{mint}_{int(rest_start.timestamp())}_{int(end.timestamp())}.json")
    return all((cache_dir / k).exists() for k in keys)


# --------------------------------------------------------------------------- #
# The re-measurement run
# --------------------------------------------------------------------------- #
def _update_run_row(state: LiveState, run_id: int, verdict: dict,
                    status: Optional[str] = None) -> None:
    """Direct UPDATE of an existing research_runs row. state.record_research_run only
    INSERTs; live progress and the terminal verdict must land on the SAME row (the
    dashboard's spinner and progress bar key off it)."""
    if status is None:
        state.conn.execute("UPDATE research_runs SET verdict_json=? WHERE id=?",
                           (json.dumps(verdict), run_id))
    else:
        state.conn.execute("UPDATE research_runs SET status=?, verdict_json=? WHERE id=?",
                           (status, json.dumps(verdict), run_id))
    state.conn.commit()


def _supersede_stale_running(state: LiveState) -> None:
    """Hygiene: any PRIOR row still status='running' is a crashed/killed run (the finally
    below normally guarantees a terminal status). Mark it failed so the dashboard spinner
    and run.py's launch guard can never wedge on a ghost."""
    rows = state.conn.execute(
        "SELECT id, verdict_json FROM research_runs WHERE status='running'").fetchall()
    for r in rows:
        try:
            v = json.loads(r["verdict_json"] or "{}")
            if not isinstance(v, dict):
                v = {}
        except (TypeError, ValueError):
            v = {}
        v["note"] = "superseded/stale"
        state.conn.execute("UPDATE research_runs SET status='failed', verdict_json=? WHERE id=?",
                           (json.dumps(v), r["id"]))
    if rows:
        state.conn.commit()
        log.warning("research: superseded %d stale 'running' row(s)", len(rows))


def run_remeasurement(state: LiveState, *, corpus_path, cache_dir, max_new_tokens: int = 400,
                      now: Optional[datetime] = None, client=None, refresh_corpus: bool = True,
                      min_n: int = 10) -> dict:
    """Full re-measurement pass. Returns the verdict dict and writes a research_runs row;
    NEVER raises (any failure -> status='failed' row + alert).

    Progress visibility: a status='running' row is inserted UP FRONT and its verdict_json is
    updated live ({phase, started_at, priced, total, n_skipped_unpriced}); the terminal
    verdict UPDATEs that SAME row in a `finally`, so a crash can never leave a 'running' row
    behind forever.

    `client`/`refresh_corpus`/`min_n` are injection points for offline tests; production
    uses the defaults (CachedPriceClient over JupiterCharts, telethon refresh, stage37's
    >=10-observations-per-side floor).
    """
    started = utcnow()
    started_iso = started.isoformat()
    verdict: dict = {"ts": started_iso, "status": "failed", "n_tokens": 0,
                     "n_new_priced": 0, "n_skipped_unpriced": 0, "champion": None,
                     "top_configs": [], "any_config_clears_gate": False,
                     "recommendation": None, "degradation_alert": False}
    # The live 'running' row (dashboard progress). A bookkeeping failure here must not block
    # the measurement itself: run_id stays None and progress updates become no-ops.
    run_id: Optional[int] = None
    try:
        _supersede_stale_running(state)
        run_id = state.record_research_run(
            ts=started, status="running",
            verdict={"phase": "corpus", "started_at": started_iso})
    except Exception:
        log.exception("research: could not create the 'running' progress row")

    progress: dict = {"phase": "corpus", "started_at": started_iso}

    def _prog(**kw) -> None:
        """Best-effort live-progress UPDATE (visibility only — never kills the run)."""
        progress.update(kw)
        if run_id is None:
            return
        try:
            _update_run_row(state, run_id, progress)
        except Exception:
            log.debug("research: progress update failed", exc_info=True)

    try:
        corpus_path, cache_dir = Path(corpus_path), Path(cache_dir)
        # pin `now` to the hour so cache keys stay stable across resumes within a run window
        now = now or utcnow().replace(minute=0, second=0, microsecond=0)

        # 1. refresh the corpus (fall back to the existing file if telethon is unavailable)
        status = "ok"
        if not refresh_corpus or not _refresh_corpus(corpus_path):
            status = "stale_corpus"
        if not corpus_path.exists():
            raise RuntimeError(f"no corpus at {corpus_path} (refresh failed, no fallback file)")

        calls = sorted([s for s in first_call_per_mint(load_corpus_json(corpus_path)) if s.mint],
                       key=lambda s: s.posted_at)
        if client is None:
            from memebot.data.cache import CachedPriceClient
            from memebot.data.jupiter import JupiterChartsClient
            client = CachedPriceClient(JupiterChartsClient(min_interval=0.4), cache_dir)

        # 2. price every call (stage14 recipe). Fetch budget: cached tokens are free;
        # NEW tokens newest-first, capped at max_new_tokens — the overflow is logged and
        # reported (no silent truncation) and gets picked up by the next run's warm cache.
        uncached = sorted((s for s in calls if not _is_cached(cache_dir, s.mint, s.posted_at, now)),
                          key=lambda s: s.posted_at, reverse=True)
        skipped_mints = {s.mint for s in uncached[max_new_tokens:]}
        new_mints = {s.mint for s in uncached[:max_new_tokens]}
        if skipped_mints:
            log.warning("research: fetch budget hit — %d uncached tokens NOT priced this run "
                        "(max_new_tokens=%d); the warm cache will absorb them next run",
                        len(skipped_mints), max_new_tokens)
        _prog(phase="pricing", priced=0, total=len(calls) - len(skipped_mints),
              n_skipped_unpriced=len(skipped_mints))
        toks = []
        n_new = 0
        n_attempted = 0
        for s in calls:
            if s.mint in skipped_mints:
                continue
            n_attempted += 1
            if n_attempted % 20 == 0:
                _prog(priced=n_attempted)
            try:
                ser = _series_to_now(client, s.mint, s.posted_at, now)
            except Exception:
                continue                            # unpriceable token -> skipped (stage37 parity)
            if not ser or not ser.candles:
                continue
            cds = [c for c in ser.candles if c.ts >= s.posted_at]
            if not cds or cds[0].open <= 0:
                continue
            H = np.array([c.high for c in cds]); L = np.array([c.low for c in cds])
            C = np.array([c.close for c in cds]); T = np.array([c.ts.timestamp() for c in cds])
            toks.append((s.mint, H, L, C, T, cds[0].open, s.posted_at.timestamp()))
            if s.mint in new_mints:
                n_new += 1
        if not toks:
            raise RuntimeError("no tokens could be priced (corpus empty or price client failing)")

        # 3. the FULL stage37 grid, 70/30 chronological train/OOS split (no resampling)
        _prog(phase="grid", priced=n_attempted)
        dates = sorted(t[6] for t in toks)
        cut = dates[int(len(dates) * 0.7)]
        rows = []
        for dip, sl, ftp, fsell, re_ in GRID:
            tr_l: list[float] = []; oo_l: list[float] = []; oo_t: list[float] = []
            for mint, H, L, C, T, sig, ts in toks:
                legs = sim(H, L, C, T, sig, dip, sl, ftp, fsell, re_)
                if not legs:
                    continue
                if ts < cut:
                    tr_l.extend(legs)
                else:
                    oo_l.extend(legs); oo_t.extend([ts] * len(legs))
            train = None
            if len(tr_l) >= min_n:
                a = np.asarray(tr_l, dtype=float)
                train = {"n": len(a), "mean": float(a.mean()), "drop3": drop_top(tr_l, 3),
                         "ci": list(mean_ci(tr_l)[1:])}
            clears, oos = (False, None)
            if len(oo_l) >= min_n:
                clears, oos = gate_pass(oo_l, oo_t)
            rows.append({"cfg": {"dip": dip, "sl": sl, "ftp": ftp, "fsell": fsell,
                                 "reentry": re_},
                         "train": train, "oos": oos, "clears": clears,
                         "_is_champion": (dip, sl, ftp, fsell, re_) == CHAMPION_CFG})

        # 4. champion health: is its OOS mean below its own historical (train) band?
        champ_row = next(r for r in rows if r["_is_champion"])
        degradation = False
        if champ_row["train"] and champ_row["oos"]:
            train_ci_lo = champ_row["train"]["ci"][0]
            degradation = bool(champ_row["oos"]["mean"] < train_ci_lo)
        champion = {"cfg": champ_row["cfg"], "train": champ_row["train"],
                    "oos": champ_row["oos"], "clears": champ_row["clears"],
                    "oos_mean_below_train_band": degradation}

        # 5. verdict. Selection is by TRAIN drop3 (robust, no OOS peeking — stage37's rule);
        # a recommendation exists ONLY if a non-champion config clears the FULL OOS gate.
        # The honest expected result is: nothing clears, recommendation stays null.
        def _train_key(r):
            d3 = r["train"]["drop3"] if r["train"] else float("nan")
            return d3 if d3 == d3 else float("-inf")       # NaN sorts last

        top8 = [{k: r[k] for k in ("cfg", "train", "oos", "clears")}
                for r in sorted(rows, key=_train_key, reverse=True)[:8]]
        clearing = [r for r in rows if r["clears"] and not r["_is_champion"]]
        recommendation = None
        if clearing:
            best = max(clearing, key=_train_key)
            recommendation = {
                "config": best["cfg"],
                "reason": "clears the FULL OOS gate (CIlo>1, drop3>1, f=2% logG>0, $500 grows). "
                          "ADVISORY ONLY — promotion requires human approval on the dashboard.",
                "evidence": {"train": best["train"], "oos": best["oos"]},
            }

        verdict.update(status=status, n_tokens=len(toks), n_new_priced=n_new,
                       n_skipped_unpriced=len(skipped_mints), champion=champion,
                       top_configs=top8,
                       any_config_clears_gate=bool(any(r["clears"] for r in rows)),
                       recommendation=recommendation, degradation_alert=degradation)

        # 6. alerts (persistence happens in the finally, on the SAME running row)
        if degradation:
            state.record_alert(severity="WARN", kind="RESEARCH_DEGRADATION",
                               message="champion OOS mean sits below its historical train band",
                               context={"champion": champion})
        log.info("research done: status=%s n_tokens=%d clears=%s degradation=%s",
                 status, len(toks), verdict["any_config_clears_gate"], degradation)
        return verdict
    except Exception as e:                          # noqa: BLE001 — must never raise out
        log.exception("research run failed")
        verdict["status"] = "failed"
        verdict["error"] = str(e)[:400]
        try:
            state.record_alert(severity="WARN", kind="RESEARCH_FAILED", message=str(e)[:300])
        except Exception:
            pass
        return verdict
    finally:
        # TERMINAL update of the SAME row, guaranteed: a crash must never leave a 'running'
        # row forever (the dashboard spinner + run.py's launch guard key off it). verdict
        # ["status"] is 'ok'/'stale_corpus' on success, 'failed' (with `error`) otherwise.
        # last_research_at is set on every outcome to avoid weekly retry storms.
        verdict["started_at"] = started_iso
        verdict["finished_at"] = utcnow().isoformat()
        try:
            if run_id is not None:
                _update_run_row(state, run_id, verdict, status=verdict["status"])
            else:                   # the running-row insert failed -> still leave one row
                state.record_research_run(ts=started, status=verdict["status"], verdict=verdict)
            state.set_system("last_research_at", started.isoformat())
        except Exception:
            log.exception("research: terminal research_runs update failed")
