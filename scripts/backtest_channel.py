#!/usr/bin/env python3
"""Phase 0.5 CLI: backtest a channel's edge by holding horizon.

Usage:
    PYTHONPATH=src python3 scripts/backtest_channel.py --corpus runs/corpus.json
    PYTHONPATH=src python3 scripts/backtest_channel.py --corpus runs/corpus.json \
        --trade-size 1.0 --latency 3 --horizons 30s,5m,1h,4h --out reports/edge.json

The corpus is a JSON list of message records: {"id", "date" (epoch or ISO), "text"[, "mint", "side"]}.
Produce one with the Telegram MCP history + memebot.ingest.telegram_mcp.save_corpus_json,
or with scripts/make_sample_corpus.py for a plumbing test against real tokens.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from memebot.config import Settings, CostModel  # noqa: E402
from memebot.data.cache import CachedPriceClient  # noqa: E402
from memebot.ingest.telegram_mcp import load_corpus_json, first_call_per_mint  # noqa: E402
from memebot.backtest.horizon_backtest import run_backtest, format_report  # noqa: E402


def _progress(i: int, n: int, sig) -> None:
    print(f"\r  pricing {i + 1}/{n}  {(sig.mint or sig.ticker or '')[:16]:<16}", end="", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Backtest a channel's edge by holding horizon (Phase 0.5).")
    ap.add_argument("--corpus", required=True, help="path to corpus JSON")
    ap.add_argument("--config", default=None, help="path to config.toml")
    ap.add_argument("--out", default=None, help="write the report JSON here")
    ap.add_argument("--horizons", default=None, help="comma list, e.g. 30s,5m,1h,4h,1d")
    ap.add_argument("--price-mode", default="close", choices=["close", "open", "high", "low"])
    ap.add_argument("--trade-size", type=float, default=None, help="override notional SOL per trade")
    ap.add_argument("--latency", type=float, default=None, help="override end-to-end latency seconds")
    ap.add_argument("--bootstrap", type=int, default=2000)
    ap.add_argument("--all-messages", action="store_true",
                    help="treat every message as a trade (default: dedup to first call per token)")
    ap.add_argument("--since", default=None, help="only calls on/after this UTC date, e.g. 2026-06-01")
    ap.add_argument("--limit-tokens", type=int, default=None, help="keep only the N most recent calls")
    ap.add_argument("--source", choices=["jupiter", "gecko"], default="jupiter",
                    help="historical OHLCV source (jupiter datapi = keyless/fast; gecko = per-pool)")
    ap.add_argument("--cache-dir", default=None, help="price-series disk cache (default data_cache/<source>)")
    ap.add_argument("--throttle", type=float, default=None, help="min seconds between requests (default per source)")
    args = ap.parse_args(argv)

    settings = Settings.load(Path(args.config) if args.config else None)
    # Apply CLI overrides to the (frozen) cost model.
    if args.trade_size is not None or args.latency is not None:
        cm = settings.cost
        cm = CostModel(
            latency_seconds=args.latency if args.latency is not None else cm.latency_seconds,
            entry_slippage_bps=cm.entry_slippage_bps, exit_slippage_bps=cm.exit_slippage_bps,
            pumpfun_fee_bps=cm.pumpfun_fee_bps, pumpswap_fee_bps=cm.pumpswap_fee_bps,
            priority_fee_sol=cm.priority_fee_sol, jito_tip_sol=cm.jito_tip_sol,
            mev_drag_bps=cm.mev_drag_bps,
            trade_size_sol=args.trade_size if args.trade_size is not None else cm.trade_size_sol,
        )
        settings = Settings(
            chain=settings.chain, gecko_network=settings.gecko_network, horizons=settings.horizons,
            unsellable_is_total_loss=settings.unsellable_is_total_loss, cost=cm, raw=settings.raw,
            birdeye_api_key=settings.birdeye_api_key, helius_api_key=settings.helius_api_key,
        )

    horizons = args.horizons.split(",") if args.horizons else None

    signals = load_corpus_json(args.corpus)
    print(f"loaded {len(signals)} messages from {args.corpus}", file=sys.stderr)

    if not args.all_messages:
        signals = first_call_per_mint(signals)
        print(f"deduped to {len(signals)} first-call-per-token signals", file=sys.stderr)
    if args.since:
        cutoff = datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc)
        signals = [s for s in signals if s.posted_at >= cutoff]
        print(f"filtered to {len(signals)} calls since {args.since}", file=sys.stderr)
    signals.sort(key=lambda s: s.posted_at)
    if args.limit_tokens and len(signals) > args.limit_tokens:
        signals = signals[-args.limit_tokens:]
        print(f"limited to most recent {len(signals)} calls", file=sys.stderr)

    cache_dir = args.cache_dir or str(ROOT / "data_cache" / args.source)
    if args.source == "jupiter":
        from memebot.data.jupiter import JupiterChartsClient
        throttle = args.throttle if args.throttle is not None else 0.4
        inner = JupiterChartsClient(min_interval=throttle)
    else:
        from memebot.data.geckoterminal import GeckoTerminalClient
        throttle = args.throttle if args.throttle is not None else 2.6
        inner = GeckoTerminalClient(network=settings.gecko_network, min_interval=throttle)
    client = CachedPriceClient(inner, cache_dir)
    print(f"source={args.source} throttle={throttle}s cache={cache_dir}", file=sys.stderr)

    report = run_backtest(
        signals, settings, client,
        horizons=horizons, price_mode=args.price_mode, bootstrap_n=args.bootstrap, progress=_progress,
    )
    print(f"\ncache: {client.hits} hits, {client.misses} fetched", file=sys.stderr)
    print("", file=sys.stderr)
    print(format_report(report))

    if args.out:
        outp = Path(args.out)
        outp.parent.mkdir(parents=True, exist_ok=True)
        outp.write_text(json.dumps(report.to_dict(), indent=2))
        print(f"\nwrote {outp}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
