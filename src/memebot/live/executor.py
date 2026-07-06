"""Executors — translate the strategy's decisions into (paper) records or (live) swaps.

The `TailRider` state machine decides WHEN to buy/sell and WHAT fraction at WHICH level, and it
already bakes config #1's fill model (entry x1.01, TP x0.985, stop x0.95) into each event's
`proceeds` (price-units per unit of original notional). So converting an event to USD is exact:

    usd_proceeds = (stake_usd / entry_price) * event.proceeds
    realized_pnl = stake_usd * (realized_multiple - 1)

`PaperExecutor` is therefore a faithful RECORDER — it realizes exactly what the machine models,
which is why paper == backtest. `LiveExecutor` (gated, INERT by default) instead places real Jupiter
swaps on a burner wallet; after the 2026-07-05 audit it is CONFIRM-THEN-COMMIT on on-chain truth (see
docs/LIVE_EXECUTION.md): a swap that does not confirm RAISES so the state machine never advances on a
phantom fill, sizing reads the real wallet balance, decimals fail closed, the kill-switch blocks buys
only, and a consecutive-failure breaker trips the kill-switch.
"""

from __future__ import annotations

import logging
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from memebot.live.strategy import Event

log = logging.getLogger("memebot.live.executor")

# A live swap that never confirms (or reverts). Raised so the engine does NOT advance the state
# machine on a phantom fill — the single most dangerous class of real-money bug the audit found.
class SwapNotConfirmed(RuntimeError):
    pass


SWAP_FAILURE_BREAKER = 3     # consecutive unconfirmed/failed swaps -> trip the kill-switch (F11)
_DUST_TOKENS = 1e-9          # a held balance below this is treated as "not held" (idempotent buy)


@dataclass
class Fill:
    mint: str
    kind: str                 # ENTRY | SELL
    price: float              # USD per token at which the fill executed
    tokens: float             # token quantity bought (ENTRY) or sold (SELL)
    usd: float                # USD spent (ENTRY) or received (SELL)
    ts: Optional[datetime] = None
    note: str = ""


class Executor(ABC):
    mode: str = "abstract"

    @abstractmethod
    def buy(self, *, mint: str, stake_usd: float, entry_price: float,
            ts: Optional[datetime] = None) -> Fill:
        """Acquire `stake_usd` of `mint`. Returns the actual fill (price, tokens, usd)."""

    @abstractmethod
    def sell_event(self, *, mint: str, stake_usd: float, entry_price: float, event: Event,
                   tokens_qty: Optional[float] = None,
                   target_remaining_tokens: Optional[float] = None) -> Fill:
        """Realize one TP/RIDE_SELL/STOP_OUT/FINALIZE event into a USD proceeds Fill.

        `target_remaining_tokens` (manual path) sizes the sell to leave exactly that many tokens
        held (idempotent absolute target), overriding the algo's `tokens_qty * remaining_frac`."""


class PaperExecutor(Executor):
    """Records exactly what the machine models (paper == backtest). No network, no re-pricing."""
    mode = "paper"

    def buy(self, *, mint, stake_usd, entry_price, ts=None) -> Fill:
        tokens = stake_usd / entry_price if entry_price > 0 else 0.0
        return Fill(mint=mint, kind="ENTRY", price=entry_price, tokens=tokens, usd=stake_usd,
                    ts=ts, note="paper entry (config #1 fill model)")

    def sell_event(self, *, mint, stake_usd, entry_price, event: Event, tokens_qty=None,
                   target_remaining_tokens=None) -> Fill:
        # event.proceeds is in price-units per unit of ORIGINAL notional; scale to this stake.
        # (tokens_qty / target_remaining_tokens are the live-executor idempotency inputs — the
        #  paper recorder realizes exactly what the machine modeled, so it ignores them.)
        usd = (stake_usd / entry_price) * event.proceeds if entry_price > 0 else 0.0
        tokens = (stake_usd / entry_price) * event.frac if entry_price > 0 else 0.0
        return Fill(mint=mint, kind="SELL", price=event.price, tokens=tokens, usd=usd,
                    ts=event.ts, note=f"paper {event.kind}")

    def sell_manual(self, *, mint, tokens: float, price: float, ts=None, note="paper manual sell") -> Fill:
        """A manual paper sell of an explicit token amount at the mark price (no cost model —
        the human's discretionary exit, simulated 1:1). Live uses LiveExecutor.sell_event."""
        tokens = max(0.0, tokens)
        return Fill(mint=mint, kind="SELL", price=price, tokens=tokens, usd=tokens * price,
                    ts=ts, note=note)


class LiveExecutor(Executor):
    """Real Jupiter Swap execution on a BURNER wallet. Ships DISABLED (inert) by default.

    Arming requires ALL of, checked on every trade:
      1. self.armed is True   (set only when env MEMEBOT_LIVE_ARMED=1)
      2. system_state.mode == 'live'
      3. kill-switch off  (BUYS only — sells are never blocked; a stop must always be able to fire)
      4. a burner WALLET_PRIVATE_KEY (allowlist-checked) + SOLANA_RPC_URL loads
      5. (real sends only) the operator gates `equivalence_ok=1` AND `dust_reconciled=1`
    Never logs or returns the private key. `dry_run=True` quotes without signing/sending.

    SAFETY: UNVERIFIED against a live wallet. The first real trade must be a dust reconciliation.
    Execution is serialized (a lock) and MUST be driven off the event loop before arming (F26).
    """
    mode = "live"

    def __init__(self, state, jupiter_client, cfg, *, armed: bool = False, dry_run: bool = True,
                 entry_slippage_bps: int = 300, exit_slippage_bps: int = 800):
        self.state = state
        self.jc = jupiter_client
        self.cfg = cfg
        self.armed = armed
        self.dry_run = dry_run
        self.entry_slippage_bps = entry_slippage_bps
        self.exit_slippage_bps = exit_slippage_bps      # F08: exits into collapsing pools need room
        self._swap = None
        self._keypair = None
        self._last_sol_usd = 0.0                         # last-good SOL/USD (audit #6 sell fallback)
        # Fine-grained lock: guards ONLY this executor's sqlite state ops (arming reads, the
        # failure breaker, lazy client init) so N concurrent workers don't race the connection.
        # The slow network + ~30s confirm run OUTSIDE it -> true cross-mint execution concurrency.
        self._state_lock = threading.Lock()

    # -- gates ------------------------------------------------------------- #
    def _require_armed(self, *, block_on_kill: bool = True) -> None:
        if not self.armed:
            raise PermissionError("LiveExecutor not armed (set MEMEBOT_LIVE_ARMED=1 to arm; inert by default)")
        if self.state.get_system("mode") != "live":
            raise PermissionError("mode is not 'live'")
        # F04: the kill-switch blocks NEW BUYS (new risk) only. A risk-reducing SELL (stop/TP/
        # finalize) must never be blocked — that would strand open positions during a drawdown.
        if block_on_kill and self.state.get_system("kill_switch") == "on":
            raise PermissionError("kill-switch is on")
        # F11: real sends require the two explicit operator gates (not settable from the dashboard).
        if not self.dry_run:
            for flag, why in (("equivalence_ok", "paper≈backtest equivalence"),
                              ("dust_reconciled", "dust-trade on-chain reconcile")):
                if self.state.get_system(flag) != "1":
                    raise PermissionError(
                        f"live gate '{flag}' not set — {why} required before real sends "
                        "(see docs/LIVE_EXECUTION.md)")

    def _ensure_clients(self):
        import os
        from memebot.live.jupiter_swap import JupiterSwap, load_burner_keypair
        if self._swap is None:
            t = getattr(self.cfg, "priority_max_lamports", None)
            self._swap = JupiterSwap(
                rpc_url=os.environ.get("SOLANA_RPC_URL", ""),
                slippage_bps=self.entry_slippage_bps,
                priority_max_lamports=int(t) if t else 2_000_000)
        if self._keypair is None and not self.dry_run:
            self._keypair = load_burner_keypair()   # allowlist-checked; raises otherwise
        return self._swap

    def _owner(self) -> Optional[str]:
        return str(self._keypair.pubkey()) if self._keypair else None

    def _sol_usd(self) -> float:
        from memebot.live.jupiter_swap import WSOL
        px = self.jc.price([WSOL]).get(WSOL) or 0.0
        if px > 0:
            self._last_sol_usd = px                      # remember for the post-confirm sell fallback
        return px

    # -- failure breaker (F11) --------------------------------------------- #
    def _on_swap_failure(self, kind: str) -> None:
        n = int(self.state.get_system("consecutive_swap_failures") or "0") + 1
        self.state.set_system("consecutive_swap_failures", str(n))
        if n >= SWAP_FAILURE_BREAKER and self.state.get_system("kill_switch") != "on":
            self.state.set_system("kill_switch", "on")
            try:
                self.state.record_alert(severity="CRIT", kind="SWAP_BREAKER",
                                        message=f"{n} consecutive {kind} swaps failed to confirm — "
                                                "kill-switch tripped (buys halted; exits still allowed)")
            except Exception:
                pass

    def _on_swap_success(self) -> None:
        if self.state.get_system("consecutive_swap_failures") not in (None, "0"):
            self.state.set_system("consecutive_swap_failures", "0")

    # -- trades ------------------------------------------------------------ #
    def buy(self, *, mint, stake_usd, entry_price, ts=None) -> Fill:
        from memebot.live.jupiter_swap import WSOL, LAMPORTS_PER_SOL
        with self._state_lock:                      # a NEW buy is new risk -> kill blocks it
            self._require_armed(block_on_kill=True)
            swap = self._ensure_clients()
        # --- network (unlocked -> concurrent across mints; per-mint serialized by engine._pending) ---
        # F54: idempotent entry — if the burner already holds this token above dust, ADOPT that
        # balance instead of sending a second buy (a crash between a landed swap and the DB commit
        # would else re-buy an illiquid microcap on the next dip candle).
        if not self.dry_run:
            dec = swap.token_decimals(mint)         # fail-closed decimals (F06)
            held_raw = swap.token_balance(self._owner(), mint)
            if held_raw and held_raw / 10 ** dec > _DUST_TOKENS:
                tokens = held_raw / 10 ** dec
                price = stake_usd / tokens if tokens else entry_price
                return Fill(mint, "ENTRY", price, tokens, stake_usd, ts=ts,
                            note="adopted on-chain balance (idempotent entry)")
        sol_usd = self._sol_usd()
        if sol_usd <= 0:
            raise RuntimeError("could not price SOL")
        lamports = int((stake_usd / sol_usd) * LAMPORTS_PER_SOL)
        q = swap.quote(WSOL, mint, lamports, slippage_bps=self.entry_slippage_bps)
        dec = swap.token_decimals(mint)             # fail-closed (F06) — never default to 6
        if self.dry_run:
            out_tokens = int(q["outAmount"]) / 10 ** dec
            price = stake_usd / out_tokens if out_tokens else entry_price
            return Fill(mint, "ENTRY", price, out_tokens, stake_usd, ts=ts,
                        note="DRY_RUN buy (no send)")
        txb = swap.build_swap(q, self._owner())
        res = swap.execute_swap(txb, self._keypair, owner_pubkey=self._owner(),
                                input_mint=WSOL, output_mint=mint)
        if not res.confirmed:                       # F01: never book an unconfirmed swap
            with self._state_lock:
                self._on_swap_failure("buy")
            raise SwapNotConfirmed(f"buy {mint} not confirmed ({res.signature})")
        with self._state_lock:
            self._on_swap_success()
        # F01: quantity from the ACTUAL on-chain out_amount, not the quote's expectation
        out_raw = res.out_amount or int(q["outAmount"])
        out_tokens = out_raw / 10 ** dec
        price = stake_usd / out_tokens if out_tokens else entry_price
        return Fill(mint, "ENTRY", price, out_tokens, stake_usd, ts=ts,
                    note=f"live buy {res.signature}")

    def sell_event(self, *, mint, stake_usd, entry_price, event: Event,
                   tokens_qty: Optional[float] = None,
                   target_remaining_tokens: Optional[float] = None) -> Fill:
        from memebot.live.jupiter_swap import WSOL, LAMPORTS_PER_SOL
        with self._state_lock:                      # F04: a SELL is never kill-blocked (buys-only)
            self._require_armed(block_on_kill=False)
            swap = self._ensure_clients()
        # --- network (unlocked -> concurrent across mints) ---
        dec = swap.token_decimals(mint)             # fail-closed (F06)
        if self.dry_run:
            if target_remaining_tokens is not None and tokens_qty is not None:
                modeled_tokens = max(0.0, tokens_qty - target_remaining_tokens)   # manual sell
            else:
                modeled_tokens = event.frac * (stake_usd / entry_price) if entry_price > 0 else 0.0
            amount_raw = int(modeled_tokens * 10 ** dec)
        else:
            # F02 + F3: size from the REAL held balance, keyed to the TARGET remaining balance
            # (not a fresh fraction-of-original) so a re-fire after a landed tx is a NO-OP —
            # idempotent. FINALIZE dumps the whole bag; a MANUAL sell targets an absolute
            # remaining balance; else sell down to tokens_qty * remaining_frac.
            held_raw = swap.token_balance(self._owner(), mint)
            if event.kind == "FINALIZE":
                amount_raw = held_raw
            elif target_remaining_tokens is not None:       # manual: absolute idempotent target
                target_raw = int(round(max(0.0, target_remaining_tokens) * 10 ** dec))
                amount_raw = max(0, held_raw - target_raw)
            elif tokens_qty is None:
                amount_raw = held_raw
            else:
                target_remaining_raw = int(round(tokens_qty * event.remaining_frac * 10 ** dec))
                amount_raw = max(0, held_raw - target_remaining_raw)
            if amount_raw <= 0:
                return Fill(mint, "SELL", event.price, 0.0, 0.0, ts=event.ts,
                            note="live sell skipped (already at/below target — idempotent)")
        q = swap.quote(mint, WSOL, amount_raw, slippage_bps=self.exit_slippage_bps)
        token_amount = amount_raw / 10 ** dec
        if self.dry_run:
            usd = (int(q["outAmount"]) / LAMPORTS_PER_SOL) * self._sol_usd()
            return Fill(mint, "SELL", event.price, token_amount, usd, ts=event.ts,
                        note="DRY_RUN sell (no send)")
        txb = swap.build_swap(q, self._owner())
        res = swap.execute_swap(txb, self._keypair, owner_pubkey=self._owner(),
                                input_mint=mint, output_mint=WSOL)
        if not res.confirmed:                       # F01: an unfilled stop/TP must NOT book as done
            with self._state_lock:
                self._on_swap_failure("sell")
            raise SwapNotConfirmed(f"sell {mint} not confirmed ({res.signature})")
        with self._state_lock:
            self._on_swap_success()
        # F4: REAL SOL received (net of fees) from the landed tx; fall back to the quote estimate
        lamports_out = res.out_amount or int(q["outAmount"])
        # audit #6: the sell already landed — a transient WSOL-price blip must NOT book $0 proceeds.
        # Use the last-good SOL/USD (set on every prior buy/sell price) rather than record a phantom loss.
        fresh = self._sol_usd()
        sol_usd = fresh or self._last_sol_usd
        usd = (lamports_out / LAMPORTS_PER_SOL) * sol_usd
        note = f"live sell {res.signature}"
        if fresh <= 0:
            note += " (proceeds estimated: SOL price unavailable)"
        return Fill(mint, "SELL", event.price, token_amount, usd, ts=event.ts, note=note)


def make_executor(mode: str, *, state=None, jupiter_client=None, cfg=None,
                  armed: bool = False, dry_run: bool = True,
                  entry_slippage_bps: int = 300, exit_slippage_bps: int = 800) -> Executor:
    """Paper by default. Live executor is inert unless `armed` (set from env MEMEBOT_LIVE_ARMED=1)."""
    if mode == "live":
        return LiveExecutor(state, jupiter_client, cfg, armed=armed, dry_run=dry_run,
                            entry_slippage_bps=entry_slippage_bps, exit_slippage_bps=exit_slippage_bps)
    return PaperExecutor()
