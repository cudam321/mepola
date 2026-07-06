#!/usr/bin/env python3
"""Seed the live SQLite DB by replaying config #1 over the whole corpus via the live engine.

This runs the SAME `TailRider` state machine the live loop uses over every first-call token
(against the warm cache), and writes real positions / closed_trades / lifecycle events / a
chronological $3-fixed bankroll curve into `runs/live_state.db`. The dashboard then shows the
REAL power-law distribution of config #1 immediately — before any live signal arrives.

Bankroll model is stage39's `bankroll_fixed_dollar` ($3/trade, start $500) — the exact model
the strategy decision was made on. Multiples come from `TailRider`, proven equal to the backtest
`sim` by tests/test_strategy_equivalence.py.

    set -a && . ./.env && set +a && PYTHONPATH=src:scripts python3 scripts/seed_live_db.py --reset
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from memebot.data.cache import CachedPriceClient           # noqa: E402
from memebot.data.jupiter import JupiterChartsClient       # noqa: E402
from memebot.ingest.telegram_mcp import first_call_per_mint, load_corpus_json  # noqa: E402
from memebot.live.executor import PaperExecutor            # noqa: E402
from memebot.live.state import LiveState                   # noqa: E402
from memebot.live.strategy import PositionState, TailRider, TailRiderConfig  # noqa: E402
from memebot.models import PriceSeries                     # noqa: E402

NOW = datetime(2026, 6, 30, 12, 0, 0, tzinfo=timezone.utc)  # pinned, matches stage14/38/39
ANSEM = "9cRCn9rGT8V2imeM2BaKs13yhMEais3ruM3rPvTGpump"
STAKE = 3.0
START = 500.0


def series_to_today(client, mint: str, t0: datetime) -> PriceSeries:
    end = min(t0 + timedelta(days=45), NOW)
    mn = client.get_price_series(mint, t0 - timedelta(minutes=5), t0 + timedelta(hours=12))
    rest_start = t0 + timedelta(hours=12)
    rest = (client.get_price_series(mint, rest_start, end)
            if end > rest_start else PriceSeries(mint, None, "hour", 1, []))
    boundary = mn.candles[-1].ts if mn.candles else t0
    candles = list(mn.candles) + [c for c in rest.candles if c.ts > boundary]
    candles.sort(key=lambda c: c.ts)
    return PriceSeries(mint=mint, pool=None, timeframe="mixed", aggregate=1, candles=candles)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", default=str(ROOT / "runs" / "your_channel_fresh.json"))
    ap.add_argument("--cache", default=str(ROOT / "data_cache" / "jupiter_untrunc"))
    ap.add_argument("--db", default=str(ROOT / "runs" / "live_state.db"))
    ap.add_argument("--reset", action="store_true", help="delete the DB first")
    ap.add_argument("--limit", type=int, default=0, help="only the first N calls (debug)")
    ap.add_argument("--window", choices=["oos", "full"], default="oos",
                    help="'oos' = the last-30%% forward-test window that validated #1 (stage39); "
                         "'full' = the whole corpus (harsher: includes the pre-ANSEM bleed)")
    ap.add_argument("--split", type=float, default=0.70, help="chronological train/OOS cut fraction")
    args = ap.parse_args()

    db_path = Path(args.db)
    if args.reset and db_path.exists():
        db_path.unlink()
        for suffix in ("-wal", "-shm"):
            p = Path(str(db_path) + suffix)
            if p.exists():
                p.unlink()

    calls = sorted([s for s in first_call_per_mint(load_corpus_json(args.corpus)) if s.mint],
                   key=lambda s: s.posted_at)
    if args.window == "oos" and len(calls) > 10:
        # the forward-test window: the last (1-split) of calls by date — what config #1 was
        # validated on (stage39). A live system trades FORWARD, so this is the representative window.
        cut = calls[int(len(calls) * args.split)].posted_at
        calls = [s for s in calls if s.posted_at >= cut]
    if args.limit:
        calls = calls[: args.limit]
    client = CachedPriceClient(JupiterChartsClient(min_interval=0.4), args.cache)
    px = PaperExecutor()
    cfg = TailRiderConfig()
    st = LiveState(db_path)
    st.set_system("mode", "paper")
    st.set_system("bankroll_start_usd", str(START))

    n_entered = n_expired = n_nodata = 0
    trades: list[dict] = []   # entered tokens in entry-time order for the bankroll pass

    for k, s in enumerate(calls):
        if k % 100 == 0:
            print(f"\r  replaying {k}/{len(calls)} (cache {client.hits}h/{client.misses}m)",
                  end="", file=sys.stderr)
        st.record_signal(ts=s.posted_at, source_channel=s.source_channel, message_id=s.message_id,
                         ticker=s.ticker, mint=s.mint, side="buy", parse_confidence=s.parse_confidence,
                         is_first_call=True, accepted=True, raw_text=(s.raw_text or "")[:400])
        st.mark_seen(s.mint, ticker=s.ticker, source_channel=s.source_channel,
                     message_id=s.message_id, first_seen_at=s.posted_at)

        try:
            ser = series_to_today(client, s.mint, s.posted_at)
        except Exception:
            ser = None
        cds = [c for c in ser.candles if c.ts >= s.posted_at] if (ser and ser.candles) else []
        if not cds or cds[0].open <= 0:
            n_nodata += 1
            st.create_position(mint=s.mint, ticker=s.ticker, signal_at=s.posted_at, signal_price=0.0,
                               state="EXPIRED", source_channel=s.source_channel, message_id=s.message_id)
            st.update_position(s.mint, close_reason="no_price_data")
            continue

        sig = cds[0].open
        tr = TailRider(cfg=cfg)
        events = []
        for c in cds:
            events.extend(tr.on_candle(c))
        events.extend(tr.finalize(cds[-1].close, cds[-1].ts))

        deadline = s.posted_at + timedelta(hours=cfg.dip_window_h)
        pid = st.create_position(mint=s.mint, ticker=s.ticker, signal_at=s.posted_at, signal_price=sig,
                                 state=tr.state.value, dip_deadline=deadline,
                                 source_channel=s.source_channel, message_id=s.message_id, t0_epoch=tr.t0)
        st.append_event(position_id=pid, mint=s.mint, ts=s.posted_at, event_type="SIGNAL",
                        price=sig, note="first-call BUY")

        if tr.state is PositionState.EXPIRED or tr.entry is None:
            n_expired += 1
            st.update_position(s.mint, close_reason="no_dip_within_48h")
            continue

        # entered -> record fills + close
        n_entered += 1
        entry = tr.entry
        tokens_qty = STAKE / entry
        mult = tr.realized_multiple
        pnl = STAKE * (mult - 1.0)
        peak_mult = (tr.peak_price / entry) if entry else None
        entry_ts = next((e.ts for e in events if e.kind == "ENTER"), s.posted_at)
        exit_ts = cds[-1].ts
        was_stopped = tr.state is PositionState.STOPPED

        for e in events:
            usd = None
            if e.kind in ("TP", "RIDE_SELL", "STOP_OUT", "FINALIZE"):
                usd = px.sell_event(mint=s.mint, stake_usd=STAKE, entry_price=entry, event=e).usd
            st.append_event(position_id=pid, mint=s.mint, ts=e.ts, event_type=e.kind, price=e.price,
                            rung_mult=e.rung_mult, frac=e.frac, proceeds_usd=usd,
                            remaining_frac=e.remaining_frac, note=e.note)

        st.update_position(s.mint, state=tr.state.value, entry_at=entry_ts.isoformat(),
                           entry_price=entry, stake_usd=STAKE, tokens_qty=tokens_qty,
                           stop_price=tr.stop_price, secured=int(tr.secured), n_tp=tr.n_tp,
                           remaining_frac=tr.rem, proceeds_units=tr.pr, peak_price=tr.peak_price,
                           current_price=cds[-1].close, current_multiple=mult, realized_multiple=mult,
                           realized_pnl_usd=pnl, closed_at=exit_ts.isoformat(),
                           close_reason=("stopped" if was_stopped else "rode_to_horizon"))
        st.record_close(position_id=pid, mint=s.mint, ticker=s.ticker, entry_at=entry_ts,
                        entry_price=entry, stake_usd=STAKE, exit_at=exit_ts,
                        close_reason=("stopped" if was_stopped else "rode_to_horizon"),
                        realized_multiple=mult, pnl_usd=pnl, peak_multiple=peak_mult,
                        held_hours=(exit_ts - entry_ts).total_seconds() / 3600.0,
                        n_tp=tr.n_tp, was_stopped=was_stopped, was_secured=tr.secured)
        trades.append(dict(ts=entry_ts, mult=mult))

    print("\r" + " " * 60 + "\r", end="", file=sys.stderr)

    # chronological bankroll curves (stage39 models), sampled after each trade:
    #   realized_equity = the CHOSEN $3-fixed sizing (can bust to $0 over a long bleed)
    #   expected_equity = a fixed-FRACTION reference (f = stake/START; mathematically can't bust)
    # Storing both makes config #1's SIZE-FRAGILITY honest and visible on the dashboard.
    trades.sort(key=lambda t: t["ts"])
    frac = STAKE / START
    b_fixed = START
    b_frac = START
    realized_pnl_cum = 0.0
    busted = False
    st.sample_bankroll(ts=(trades[0]["ts"] if trades else NOW), realized_equity_usd=b_fixed,
                       unrealized_equity_usd=b_fixed, deployed_usd=0.0, dry_powder_usd=b_fixed,
                       n_open=0, n_watching=0, realized_pnl_cum_usd=0.0,
                       expected_equity_usd=b_frac, expected_lo_usd=b_frac, expected_hi_usd=b_frac)
    for t in trades:
        if not busted:
            s_amt = min(STAKE, b_fixed)
            realized_pnl_cum += s_amt * (t["mult"] - 1.0)
            b_fixed = b_fixed - s_amt + s_amt * t["mult"]
            if b_fixed <= 1e-6:
                b_fixed = 0.0
                busted = True
        b_frac = b_frac * (1 - frac) + b_frac * frac * t["mult"]
        st.sample_bankroll(ts=t["ts"], realized_equity_usd=b_fixed, unrealized_equity_usd=b_fixed,
                           deployed_usd=0.0, dry_powder_usd=b_fixed, n_open=0, n_watching=0,
                           realized_pnl_cum_usd=realized_pnl_cum,
                           expected_equity_usd=b_frac, expected_lo_usd=b_frac, expected_hi_usd=b_frac)
    b = b_fixed

    st.set_system("seeded_at", NOW.isoformat())
    st.set_system("seed_source", Path(args.corpus).name)

    # summary
    closed = st.closed_trades()
    mults = sorted((c["realized_multiple"] for c in closed), reverse=True)
    ansem = next((c["realized_multiple"] for c in closed if c["mint"] == ANSEM), None)
    wins = sum(1 for m in mults if m > 1)
    top1_pnl = (mults[0] - 1) * STAKE if mults else 0.0
    total_pnl = sum((m - 1) * STAKE for m in mults)
    gross_gains = sum((m - 1) * STAKE for m in mults if m > 1)
    print("=" * 78)
    print(f"  SEEDED {args.db}")
    print(f"  calls={len(calls)}  entered={n_entered}  no_dip={n_expired}  no_data={n_nodata}")
    print(f"  win%={100*wins/max(1,len(mults)):.1f}  best={mults[0] if mults else 0:.1f}x  "
          f"ANSEM={ansem or 0:.1f}x")
    print(f"  ${STAKE:.0f}-fixed bankroll : $500 -> ${b_fixed:.0f}   (size-fragile: fixed-$ can bust)")
    print(f"  {frac*100:.2f}%-fractional   : $500 -> ${b_frac:.0f}   (survivable: fraction can't bust)")
    if gross_gains:
        print(f"  concentration: top1 token = {100*top1_pnl/gross_gains:.0f}% of all winners' P&L "
              f"(the honest power-law reality)")
    print("=" * 78)
    st.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
