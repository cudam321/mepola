#!/usr/bin/env python3
"""Dust reconcile — the deliberate, operator-run pre-live verification (a real $1 round-trip).

The first real use of the live path MUST be a single tiny trade, executed and reconciled on-chain
against the recorded fills, BEFORE flipping `dust_reconciled=1` for production (see
docs/LIVE_EXECUTION.md). This script does exactly that: buy a dust amount of a liquid mint, confirm it,
read the REAL received tokens back from the chain, sell it all, and check the balances reconcile.

SAFETY:
  * Default is DRY-RUN (quotes + on-chain reads only, nothing signed or sent). Add --send to trade.
  * --send additionally requires env MEMEBOT_LIVE_ARMED=1 and typing the confirmation phrase.
  * Signs ONLY with the sanctioned burner (jupiter_swap enforces the pubkey allowlist).
  * Uses a SCRATCH sqlite db — it never touches runs/live_state.db.

Usage (from repo root):
  set -a && . ./.env && set +a
  # dry-run first (safe):
  PYTHONPATH=src python scripts/dust_reconcile.py --mint <MINT> --usd 1.0
  # then the real round-trip:
  MEMEBOT_LIVE_ARMED=1 PYTHONPATH=src python scripts/dust_reconcile.py --mint <MINT> --usd 1.0 --send
"""

from __future__ import annotations

import argparse
import os
import tempfile
import time
from datetime import datetime, timezone

from memebot.data.jupiter import JupiterClient
from memebot.live.executor import LiveExecutor, SwapNotConfirmed
from memebot.live.jupiter_swap import WSOL
from memebot.live.state import LiveState
from memebot.live.strategy import Event, TailRiderConfig

CONFIRM_PHRASE = "send the dust trade"
MAX_USD = 3.0                     # a reconcile trade should be tiny — never more than one stake


def _bal(swap, owner, mint) -> float:
    dec = swap.token_decimals(mint)
    return swap.token_balance(owner, mint) / (10 ** dec)


def _sol(swap, owner) -> float:
    res = swap._rpc("getBalance", [owner])
    return (res.get("value", 0) or 0) / 1e9


def _bal_settle(swap, owner, mint, baseline: float, *, timeout: float = 30.0) -> float:
    """Poll the on-chain token balance until it DIFFERS from `baseline`, or until timeout. A swap
    that is 'finalized' per getSignatureStatuses can still read STALE on a load-balanced RPC for a
    few seconds (different nodes at different slots) — the first reconcile mis-read 0 tokens right
    after a successful buy for exactly this reason. Returns the settled balance."""
    import time
    end = time.time() + timeout
    last = _bal(swap, owner, mint)
    while time.time() < end and abs(last - baseline) <= 1e-12:
        time.sleep(1.5)
        last = _bal(swap, owner, mint)
    return last


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mint", required=True, help="a liquid SPL mint to round-trip")
    ap.add_argument("--usd", type=float, default=1.0, help="dust size in USD (<= %.1f)" % MAX_USD)
    ap.add_argument("--send", action="store_true", help="ACTUALLY sign+send (default is dry-run)")
    ap.add_argument("--sell-only", action="store_true",
                    help="skip the buy; just sell the burner's existing balance of --mint (recovery)")
    args = ap.parse_args()

    if args.usd <= 0 or args.usd > MAX_USD:
        raise SystemExit(f"--usd must be in (0, {MAX_USD}]")
    send = args.send or args.sell_only        # --sell-only is a real sell -> same gating as --send
    if send:
        if os.environ.get("MEMEBOT_LIVE_ARMED") != "1":
            raise SystemExit("refusing a real send without MEMEBOT_LIVE_ARMED=1 in the environment")
        if not os.environ.get("WALLET_PRIVATE_KEY"):
            raise SystemExit("WALLET_PRIVATE_KEY (the sanctioned burner) must be set")
        action = "RECOVERY SELL of the existing balance" if args.sell_only \
            else f"${args.usd:.2f} ROUND-TRIP"
        print(f"\n⚠️  ABOUT TO SEND A REAL {action} on the burner wallet.")
        print(f'    Type exactly: {CONFIRM_PHRASE!r} to proceed.')
        if input("    > ").strip() != CONFIRM_PHRASE:
            raise SystemExit("aborted — phrase did not match")

    scratch = tempfile.mktemp(suffix=".db")
    st = LiveState(scratch)
    st.set_system("mode", "live")
    st.set_system("kill_switch", "off")
    # the script IS the deliberate dust trade -> clear the two operator gates on the SCRATCH db only
    st.set_system("equivalence_ok", "1")
    st.set_system("dust_reconciled", "1")

    jc = JupiterClient(api_key=os.environ.get("JUPITER_API_KEY") or None)
    ex = LiveExecutor(st, jc, TailRiderConfig(), armed=True, dry_run=not send)
    swap = ex._ensure_clients()
    owner = ex._owner()
    dec = swap.token_decimals(args.mint)

    mode = "SEND (real)" if send else "DRY-RUN (no send)"
    print(f"\n=== dust reconcile [{mode}] — mint {args.mint[:6]}…{args.mint[-4:]}, ${args.usd:.2f} ===")
    print(f"SOL price: ${ex._sol_usd():.2f}")

    if not send:
        fill = ex.buy(mint=args.mint, stake_usd=args.usd, entry_price=0.0)
        print(f"[dry] buy  -> {fill.tokens:,.6g} tokens quoted, note='{fill.note}'")
        # dry-run sell sizes from the modeled entry (a real send sizes from the wallet), so feed
        # the buy's fill price so the sell quotes a sensible amount instead of 0.
        ev = Event(ts=datetime.now(timezone.utc), kind="FINALIZE", price=fill.price,
                   frac=1.0, remaining_frac=0.0)
        s = ex.sell_event(mint=args.mint, stake_usd=args.usd, entry_price=fill.price, event=ev)
        print(f"[dry] sell -> ${s.usd:.4f} quoted, note='{s.note}'")
        print("\nDRY-RUN OK — quotes + on-chain reads succeeded. Re-run with --send for the real trade.")
        st.close(); os.remove(scratch)
        return 0

    # NOTE: _bal() returns the DECIMAL-ADJUSTED balance, and buy.tokens is decimal-adjusted too —
    # everything below is in adjusted "tokens", never raw (mixing the two is a unit bug).
    if args.sell_only:
        # RECOVERY: sell whatever the burner currently holds of --mint (e.g. a bag stranded by an
        # earlier eager-read reconcile). No buy.
        held0 = _bal(swap, owner, args.mint)
        print(f"sell-only: burner holds {held0:,.6g} {args.mint[:4]}")
        if held0 <= 0:
            print("nothing to sell."); st.close(); os.remove(scratch); return 0
        ev = Event(ts=datetime.now(timezone.utc), kind="FINALIZE", price=0.0, frac=1.0, remaining_frac=0.0)
        try:
            s = ex.sell_event(mint=args.mint, stake_usd=args.usd, entry_price=0.0, event=ev)
        except SwapNotConfirmed as e:
            raise SystemExit(f"SELL did not confirm: {e} — still holding; retry")
        held1 = _bal_settle(swap, owner, args.mint, held0)      # poll until the sell reflects
        print(f"sell confirmed: got ${s.usd:.4f}; balance now {held1:,.6g} {args.mint[:4]}")
        print("✅ recovered." if held1 <= held0 * 0.02 else "⚠️  balance not flat — check on-chain.")
        st.close(); os.remove(scratch)
        return 0

    sol0, tok0 = _sol(swap, owner), _bal(swap, owner, args.mint)
    print(f"before: {sol0:.6f} SOL, {tok0:,.6g} {args.mint[:4]}")

    # audit #30(a): the executor's idempotent-entry guard ADOPTS any existing balance above dust
    # instead of a real swap. If the burner already holds --mint the round-trip buy would move no
    # funds, the balance would not change, and bought_ok would compute a spurious mismatch (false
    # "DID NOT RECONCILE"), and the FINALIZE would dump the whole pre-existing bag. Abort clearly.
    if tok0 > 1e-9:
        raise SystemExit(f"burner already holds {tok0:,.6g} {args.mint[:4]} — the round-trip buy would "
                         "be ADOPTED (no real swap), giving a false mismatch. Pick a mint you do NOT "
                         "hold, or run --sell-only first to flatten it.")
    try:
        buy = ex.buy(mint=args.mint, stake_usd=args.usd, entry_price=0.0)
    except SwapNotConfirmed as e:
        # audit #30(b): the tx MAY still land after the confirm window — do NOT claim funds untouched.
        raise SystemExit(f"BUY did not confirm: {e} — the tx MAY still have landed; CHECK the on-chain "
                         "balance / run --sell-only before assuming funds are untouched")
    # poll until the (finalized) buy reflects on-chain — a load-balanced RPC can read stale for
    # seconds even after 'finalized', which mis-read 0 on the first attempt.
    tok1 = _bal_settle(swap, owner, args.mint, tok0)
    print(f"buy confirmed: recorded {buy.tokens:,.6g} tokens; on-chain balance now "
          f"{tok1:,.6g} (Δ {tok1 - tok0:+,.6g})  {buy.note}")

    ev = Event(ts=datetime.now(timezone.utc), kind="FINALIZE", price=0.0, frac=1.0, remaining_frac=0.0)
    try:
        sell = ex.sell_event(mint=args.mint, stake_usd=args.usd, entry_price=0.0, event=ev)
    except SwapNotConfirmed as e:
        raise SystemExit(f"SELL did not confirm: {e} — you still hold {tok1:,.6g} "
                         f"{args.mint[:4]}; run --sell-only to recover, investigate before arming")
    tok2 = _bal_settle(swap, owner, args.mint, tok1)           # poll until the sell reflects
    sol2 = _sol(swap, owner)
    print(f"sell confirmed: got ${sell.usd:.4f}; on-chain balance now {tok2:,.6g} "
          f"tokens, {sol2:.6f} SOL")

    # reconciliation checks — all in decimal-adjusted tokens (the settled reads are on-chain truth)
    eps = max(buy.tokens * 1e-4, 1e-6)
    bought_ok = abs((tok1 - tok0) - buy.tokens) <= max(buy.tokens * 0.02, eps)
    sold_ok = tok2 <= tok0 + max(buy.tokens * 0.02, eps)   # ended ~back to the starting bag
    print("\n--- reconciliation ---")
    print(f"  buy tokens match on-chain delta : {'OK' if bought_ok else 'MISMATCH'}")
    print(f"  sold the whole bag (flat token) : {'OK' if sold_ok else 'MISMATCH'}")
    print(f"  round-trip SOL cost             : {sol0 - sol2:+.6f} SOL (fees + spread)")
    if bought_ok and sold_ok:
        print("\n✅ RECONCILED. If you are ready to trade live, set the gate on the REAL db:")
        print('   PYTHONPATH=src python -c "from memebot.live.state import LiveState; '
              "s=LiveState('runs/live_state.db'); s.set_system('equivalence_ok','1'); "
              "s.set_system('dust_reconciled','1'); print('gates set')\"")
        print("   then set MEMEBOT_LIVE_ARMED=1 and MEMEBOT_LIVE_SEND=1 on the deploy.")
    else:
        print("\n❌ DID NOT RECONCILE — do NOT arm. Investigate the mismatch above.")
    st.close(); os.remove(scratch)
    return 0 if (bought_ok and sold_ok) else 1


if __name__ == "__main__":
    raise SystemExit(main())
