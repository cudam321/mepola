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
import time
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
_BALANCE_RETRY_S = 2.0       # re-read delay when a sell sees NO token account (RPC-lag ambiguity)
_MINT_CACHE_CAP = 256        # F31: bound the per-mint truth caches (oldest-first eviction)


def _trim_cache(d: dict, cap: int = _MINT_CACHE_CAP) -> None:
    while len(d) > cap:
        d.pop(next(iter(d)))


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
        # Per-mint memory of on-chain truth (dict ops are GIL-atomic; per-mint access is already
        # serialized by engine._pending, so no extra lock):
        # _post_bal: the owner's raw token balance AFTER our last confirmed swap, parsed from the
        #   tx itself — authoritative at that slot, immune to RPC read-lag (the lagged-read class).
        # _unconfirmed_sell: mint -> {signature: recovery_attempts} for sells whose confirm timed
        #   out — if one actually landed, the next leg recovers its REAL proceeds instead of
        #   booking a $0 idempotent skip. A dict (not one slot) so an unresolved sig is never
        #   overwritten by the next timeout; sigs are removed only when a fill commits.
        self._post_bal: dict[str, int] = {}
        self._unconfirmed_sell: dict[str, dict[str, int]] = {}
        # _unconfirmed_buy: signature of a BUY whose confirm timed out — if it actually landed,
        #   the next ENTER adopts its on-chain tokens instead of buying a second bag (H2: the
        #   double-buy mirror of the $0-sell incident).
        self._unconfirmed_buy: dict[str, str] = {}

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
            try:                                         # M3: persist across restarts, so the first
                with self._state_lock:                   # action after a boot-time price blip never
                    self.state.set_system("last_sol_usd", f"{px:.6f}")   # books $0 proceeds
            except Exception:
                pass
        return px

    def _sol_usd_safe(self) -> float:
        """_sol_usd that NEVER raises — for any point where money has already moved (AUDIT B1).
        Falls back to the last-good price (persisted across restarts, M3); 0.0 only if no price
        was ever observed in the DB's lifetime."""
        try:
            px = self._sol_usd() or self._last_sol_usd
        except Exception:
            px = self._last_sol_usd
        if px <= 0:
            try:
                with self._state_lock:
                    px = float(self.state.get_system("last_sol_usd") or 0.0)
                self._last_sol_usd = px or self._last_sol_usd
            except Exception:
                pass
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

    def _book_fee(self, res) -> None:
        """M2: accumulate the tx fee (base+priority, from the landed tx) into a running total the
        equity invariant subtracts — else ~$0.3-0.6/position of real cost reads as book drift.
        Fail-safe: never raises (called post-confirm)."""
        try:
            from memebot.live.jupiter_swap import LAMPORTS_PER_SOL
            fee = getattr(res, "fee_lamports", 0) or 0
            if fee <= 0:
                return
            usd = (fee / LAMPORTS_PER_SOL) * self._sol_usd_safe()   # never raises; DB fallback
            if usd <= 0:
                return
            with self._state_lock:
                cur = float(self.state.get_system("cum_onchain_fees_usd") or 0.0)
                self.state.set_system("cum_onchain_fees_usd", f"{cur + usd:.4f}")
        except Exception:
            log.exception("fee booking failed (fill unaffected)")

    def _leg_drift_check(self, mint: str, expected_post_raw: int, res,
                         amount_raw: int = 0) -> None:
        """Per-leg verification: the confirmed tx's postTokenBalances vs the target the leg was
        sized to. Any future sizing/booking bug becomes a ONE-leg incident: CRIT + kill-switch
        (buys halted; exits always allowed) instead of a ladder-wide bleed. The leg is already
        booked from on-chain truth — this only raises the alarm, it never blocks the fill.

        M8 (audit 2026-07-07): the two failure modes are separable via the landed in_amount —
        if the tx moved EXACTLY the tokens the leg was sized to, the leg itself is correct and
        any post-balance mismatch means the PRE-read was stale (restart, empty cache): WARN,
        no kill-switch. Only a leg that moved the WRONG amount is a real sizing bug: CRIT."""
        if getattr(res, "post_balance", -1) < 0:
            return                                   # tx unparsed — the recon monitor still covers it
        tol = max(int(expected_post_raw * 0.05), 10)  # 5% (decimals rounding across legs) or dust
        if abs(res.post_balance - expected_post_raw) <= tol:
            return
        landed_in = getattr(res, "in_amount", 0) or 0
        leg_exact = amount_raw > 0 and abs(landed_in - amount_raw) <= max(int(amount_raw * 0.01), 10)
        with self._state_lock:
            if leg_exact:
                try:
                    self.state.record_alert(
                        severity="WARN", kind=f"LEG_PRE_READ_STALE_{mint[:8]}",
                        message=f"{mint[:6]}… sell leg moved exactly its sized {landed_in} raw "
                                f"but post-balance {res.post_balance} ≠ modeled "
                                f"{expected_post_raw} — the pre-trade balance read was stale; "
                                "book reconciles from on-chain truth")
                except Exception:
                    pass
                return
            self.state.set_system("kill_switch", "on")
            try:
                self.state.record_alert(
                    severity="CRIT", kind=f"LEG_DRIFT_{mint[:8]}",
                    message=f"{mint[:6]}… sell leg landed but post-balance "
                            f"{res.post_balance} ≠ target {expected_post_raw} — sizing/booking "
                            "drift; kill-switch tripped (buys halted; exits still allowed)")
            except Exception:
                pass

    # -- unconfirmed-sell recovery (B2, reworked) ---------------------------- #
    def _resolve_prior_sells(self, swap, mint: str, dec: int) -> tuple[float, float, list[str]]:
        """Check every stashed confirm-timeout sell sig against the chain. Returns
        (recovered_usd, recovered_tokens, resolved_sigs). resolved_sigs are sigs whose fate is
        now KNOWN (landed → proceeds included; found-but-reverted; or given up after 5 tries) —
        the caller removes them via _commit_sell_recovery ONLY when a fill is returned, so a
        raise anywhere in between leaves the stash intact for the next leg."""
        from memebot.live.jupiter_swap import LAMPORTS_PER_SOL, WSOL
        recovered_usd = recovered_tokens = 0.0
        resolved: list[str] = []
        stash = self._unconfirmed_sell.get(mint) or {}
        for sig, attempts in list(stash.items()):
            try:
                prev_in, prev_out, prev_post = swap.landed_amounts_ex(
                    sig, self._owner(), mint, WSOL)
            except Exception:
                stash[sig] = attempts + 1
                if stash[sig] >= 5:
                    log.warning("giving up resolving sell %s for %s after %d attempts",
                                sig, mint, stash[sig])
                    resolved.append(sig)
                continue
            if prev_out > 0:                 # landed: real SOL arrived, real tokens left
                recovered_usd += (prev_out / LAMPORTS_PER_SOL) * self._sol_usd_safe()
                recovered_tokens += prev_in / 10 ** dec
                resolved.append(sig)
            elif prev_post >= 0:             # tx FOUND on-chain but moved nothing -> reverted
                resolved.append(sig)
            else:                            # not found (yet) — may still land inside the
                stash[sig] = attempts + 1    # blockhash window; re-check next leg, bounded
                if stash[sig] >= 5:
                    resolved.append(sig)     # long past the validity window — it can't land
        return recovered_usd, recovered_tokens, resolved

    def _commit_sell_recovery(self, mint: str, resolved_sigs: list[str]) -> None:
        """Remove now-booked/known sigs from the stash. Called ONLY at fill-return points."""
        stash = self._unconfirmed_sell.get(mint)
        if not stash:
            return
        for sig in resolved_sigs:
            stash.pop(sig, None)
        if not stash:
            self._unconfirmed_sell.pop(mint, None)

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
            # H2 (audit 2026-07-07): resolve a prior confirm-timeout BUY first — the tx itself
            # (not a lag-prone balance read) says whether it landed. If it did, adopt its tokens;
            # a fresh balance read alone can miss a just-landed buy and double-buy the microcap.
            prev_sig = self._unconfirmed_buy.pop(mint, None)
            if prev_sig:
                try:
                    _pi, prev_out, prev_post = swap.landed_amounts_ex(
                        prev_sig, self._owner(), WSOL, mint)
                except Exception:
                    # Audit #2 (re-audit): with the predecessor's fate UNKNOWN we must not send
                    # a second buy — the RPC that failed this read is the same degraded RPC the
                    # follow-up balance read would use (correlated). Fail closed, retry next tick.
                    self._unconfirmed_buy[mint] = prev_sig
                    raise RuntimeError(
                        f"buy {mint}: prior unconfirmed buy {prev_sig} unresolvable right now — "
                        "refusing to risk a double buy; retrying next tick")
                if prev_out > 0 or prev_post > 0:
                    raw = prev_post if prev_post > 0 else prev_out
                    self._post_bal[mint] = raw
                    _trim_cache(self._post_bal)
                    tokens = raw / 10 ** dec
                    price = stake_usd / tokens if tokens else entry_price
                    return Fill(mint, "ENTRY", price, tokens, stake_usd, ts=ts,
                                note=f"adopted late-landed buy {prev_sig}")
            held_raw, _n_accounts = swap.token_balance_ex(self._owner(), mint)
            cached_post = self._post_bal.get(mint)
            if cached_post is not None:
                # for ADOPTION the higher figure is the safe one: never re-buy tokens our own
                # confirmed tx already put in the wallet just because a lagged node hides them
                held_raw = max(held_raw, cached_post)
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
            self._unconfirmed_buy[mint] = res.signature   # may still land — next ENTER adopts it
            _trim_cache(self._unconfirmed_buy)
            with self._state_lock:
                self._on_swap_failure("buy")
            raise SwapNotConfirmed(f"buy {mint} not confirmed ({res.signature})")
        # Re-audit #4: NOTHING may raise past this point — the buy CONFIRMED (money moved); an
        # exception here marks a landed ENTER failed → rollback → re-fire → possible double buy.
        # Mirrors the sell side's B1 hardening; each post-confirm step is individually fail-safe.
        try:
            with self._state_lock:
                self._on_swap_success()
        except Exception:
            log.exception("post-confirm breaker reset failed (buy fill still booked)")
        if getattr(res, "post_balance", -1) >= 0:    # remember on-chain truth for the next sell
            self._post_bal[mint] = res.post_balance
            _trim_cache(self._post_bal)
        self._book_fee(res)                          # M2: real cost into the invariant's ledger
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
            # AUDIT B2 (2026-07-07, reworked in the re-audit): resolve prior confirm-timeout
            # sells FIRST — if one landed, its proceeds fold into THIS leg's fill no matter what
            # this leg turns out to be. The stash is only COMMITTED (sigs removed) when a fill is
            # actually returned; any raise between here and the return leaves it intact for the
            # next leg (the old pop-first flow lost landed cash when the next leg itself failed).
            recovered_usd, recovered_tokens, resolved_sigs = self._resolve_prior_sells(
                swap, mint, dec)
            fresh_raw, n_accounts = swap.token_balance_ex(self._owner(), mint)
            cached_post = self._post_bal.get(mint)
            held_raw = fresh_raw
            # phantom-stop incident (2026-07-07): 64s after a confirmed live buy, the stop's balance
            # read hit an RPC node with NO token account visible yet (unindexed fresh ATA); the
            # 0 was taken as "the sell already landed" and the leg booked a $0-proceeds close
            # while the full just-bought bag sat in the wallet. A zero read with NO account is
            # AMBIGUOUS — use our last confirmed tx's post-balance if we have one (authoritative),
            # else re-read once, then RAISE so the engine rolls back and retries next tick.
            if held_raw <= 0 and n_accounts == 0:
                if cached_post is not None and cached_post > 0:
                    held_raw = cached_post
                else:
                    time.sleep(_BALANCE_RETRY_S)
                    held_raw, n_accounts = swap.token_balance_ex(self._owner(), mint)
                    if held_raw <= 0 and n_accounts == 0:
                        raise RuntimeError(
                            f"sell {mint}: no token account visible — ambiguous zero-balance read "
                            "(RPC lag?); refusing to book a $0 sell")
            elif cached_post is not None:
                # Prefer the SMALLER of (fresh read, last confirmed post-balance): a stale-HIGH
                # read oversells (the release incident); a cache gone high (operator sold from
                # the burner directly) is corrected by the fresh read. Never-oversell either way.
                held_raw = min(held_raw, cached_post)

            def _size(h: int) -> int:
                """This leg's raw sell amount for a given held balance (target-keyed, clamped)."""
                if event.kind == "FINALIZE":
                    return h
                if target_remaining_tokens is not None:     # manual: absolute idempotent target
                    return max(0, h - int(round(max(0.0, target_remaining_tokens) * 10 ** dec)))
                if tokens_qty is None:
                    # M4: a PARTIAL leg (a 33% TP, a 25% rung) arriving without its sizing key is
                    # a caller bug — fail CLOSED, never fail open into a full-bag market dump.
                    if event.frac is not None and event.frac < 0.999:
                        raise RuntimeError(f"sell {mint}: partial {event.kind} leg missing "
                                           "tokens_qty — refusing to size it as a full dump")
                    return h
                a = max(0, h - int(round(tokens_qty * event.remaining_frac * 10 ** dec)))
                # ladder-replay incident (2026-07-07): a STALE-high read + target-keyed sizing would
                # OVERSELL past this leg's share. One leg never sells more than its modeled size.
                if event.frac:
                    a = min(a, int(round(tokens_qty * event.frac * 10 ** dec)))
                return a

            amount_raw = _size(held_raw)
            if amount_raw <= 0:
                # Re-audit CRIT #1: the LOW side says "already at/below target", but a VISIBLE
                # empty ATA on a lagged node (token accounts are never closed by a full sell) —
                # or a stale-low cache that outlived a close — reads exactly the same. Before
                # booking a $0 leg, check the HIGH side: if any source says a real sell remains,
                # the zero is ambiguous. Two agreeing fresh reads beat a disagreeing cache.
                high_raw = max(fresh_raw, cached_post or 0)
                if _size(high_raw) > 0:
                    time.sleep(_BALANCE_RETRY_S)
                    re_raw, _re_n = swap.token_balance_ex(self._owner(), mint)
                    if _size(re_raw) > 0:
                        held_raw = re_raw               # confirmed by a second independent read
                        self._post_bal.pop(mint, None)  # the cache was the stale-low source
                        cached_post = None
                        amount_raw = _size(held_raw)
                    else:
                        raise RuntimeError(
                            f"sell {mint}: balance sources disagree (read {fresh_raw}, cache "
                            f"{cached_post}, re-read {re_raw}) — ambiguous; refusing to book "
                            "a $0 sell")
            expected_post_raw = held_raw - amount_raw   # what the wallet must show after this leg
            if amount_raw <= 0:
                self._commit_sell_recovery(mint, resolved_sigs)
                if recovered_usd > 0:      # the late-landed prior leg IS this fill (B2)
                    return Fill(mint, "SELL", event.price, recovered_tokens, recovered_usd,
                                ts=event.ts,
                                note="recovered proceeds from late-landed sell(s) "
                                     f"{','.join(resolved_sigs)}")
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
            # may still land — next leg recovers it; NEVER overwrites an older unresolved sig
            self._unconfirmed_sell.setdefault(mint, {})[res.signature] = 0
            _trim_cache(self._unconfirmed_sell)
            self._post_bal.pop(mint, None)                 # wallet state unknown until resolved
            with self._state_lock:
                self._on_swap_failure("sell")
            raise SwapNotConfirmed(f"sell {mint} not confirmed ({res.signature})")
        # AUDIT B1 (2026-07-07): NOTHING may raise past this point — the money has moved. An
        # exception here marks a LANDED leg as failed; the engine rolls back and re-fires, and
        # the retry books $0 (incident #1 through a different door). Every post-confirm step is
        # individually fail-safe; the Fill below is ALWAYS returned.
        try:
            with self._state_lock:
                self._on_swap_success()
        except Exception:
            log.exception("post-confirm breaker reset failed (fill still booked)")
        self._commit_sell_recovery(mint, resolved_sigs)   # booked below; unresolved sigs SURVIVE
        if event.kind == "FINALIZE":
            # position is closing — a 0-balance cache outliving it would poison a future manual
            # re-buy of the same mint via min(fresh, stale-0) (re-audit CRIT #1 variant b)
            self._post_bal.pop(mint, None)
        elif getattr(res, "post_balance", -1) >= 0:  # remember on-chain truth for the next leg
            self._post_bal[mint] = res.post_balance
        else:
            self._post_bal.pop(mint, None)           # our own sell invalidated the old cache
        try:
            self._leg_drift_check(mint, expected_post_raw, res, amount_raw=amount_raw)
        except Exception:
            log.exception("leg drift check failed (fill still booked)")
        self._book_fee(res)                          # M2: real cost into the invariant's ledger
        # F4: REAL SOL received (net of fees) from the landed tx; fall back to the quote estimate
        lamports_out = res.out_amount or int(q["outAmount"])
        # audit #6: the sell already landed — a transient WSOL-price blip must NOT book $0 proceeds.
        # Use the last-good SOL/USD (set on every prior buy/sell price) rather than record a phantom loss.
        sol_usd = self._sol_usd_safe()               # B1: never raises after the money moved
        usd = (lamports_out / LAMPORTS_PER_SOL) * sol_usd
        note = f"live sell {res.signature}"
        if sol_usd <= 0:
            note += " (proceeds estimated: SOL price unavailable)"
        if recovered_usd > 0:                        # B2: fold the late-landed prior leg's cash in
            usd += recovered_usd
            token_amount += recovered_tokens
            note += (f" (+${recovered_usd:.2f} recovered from late-landed "
                     f"{','.join(resolved_sigs)})")
        return Fill(mint, "SELL", event.price, token_amount, usd, ts=event.ts, note=note)


def make_executor(mode: str, *, state=None, jupiter_client=None, cfg=None,
                  armed: bool = False, dry_run: bool = True,
                  entry_slippage_bps: int = 300, exit_slippage_bps: int = 800) -> Executor:
    """Paper by default. Live executor is inert unless `armed` (set from env MEMEBOT_LIVE_ARMED=1)."""
    if mode == "live":
        return LiveExecutor(state, jupiter_client, cfg, armed=armed, dry_run=dry_run,
                            entry_slippage_bps=entry_slippage_bps, exit_slippage_bps=exit_slippage_bps)
    return PaperExecutor()
