"""Jupiter Swap client for LIVE execution on a BURNER wallet. Isolated, dangerous, gated.

This is the only module that can move real funds. It is built to the Jupiter Swap v1 (lite-api)
spec: GET /quote -> POST /swap (returns a base64 VersionedTransaction) -> sign with the burner key
-> send via the configured RPC -> confirm -> read the LANDED amounts back from the chain. It NEVER
logs or returns the private key.

Dependencies (`solders`) are imported lazily so the module imports without the `solana` extra; only
`execute_swap` needs them. `quote` and `build_swap` are read-only and can be dry-run to validate
connectivity BEFORE any key is loaded. Nothing here runs unless the caller has already passed the
LiveExecutor arming gates.

SAFETY: this path is UNVERIFIED against a live wallet in this build. The first real use must be a
single dust trade reconciled on-chain against the paper model (see docs/LIVE_EXECUTION.md) before any
size. `load_burner_keypair` asserts the loaded pubkey equals the sanctioned burner and raises
otherwise; a previously user-pasted key is COMPROMISED — never use or fund it.
"""

from __future__ import annotations

import base64
import os
import threading
from dataclasses import dataclass
from typing import Optional

import httpx

WSOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SWAP_BASE = "https://lite-api.jup.ag/swap/v1"
LAMPORTS_PER_SOL = 1_000_000_000

# The ONE wallet this bot may ever sign with (F09). load_burner_keypair fails closed if the loaded
# key's pubkey is anything else — the belt-and-braces against ever signing with the wrong wallet.
# Set MEMEBOT_BURNER_PUBKEY to your burner's public key; unset = no key can ever load (fail closed).
EXPECTED_BURNER_PUBKEY = os.environ.get("MEMEBOT_BURNER_PUBKEY", "")


@dataclass
class SwapResult:
    signature: str
    in_amount: int          # ACTUAL raw units of input mint moved (from the landed tx), 0 if unknown
    out_amount: int         # ACTUAL raw units of output mint received (from the landed tx), 0 if unknown
    confirmed: bool
    # Owner's raw balance of the TOKEN side AFTER the tx, from the confirmed tx's
    # postTokenBalances — the authoritative wallet state at that slot, immune to RPC read
    # lag (the lagged-read class). -1 = unknown/unparsed (0 is a real balance).
    post_balance: int = -1
    # The tx fee actually paid (base + priority), from meta.fee. Booked cumulatively so
    # ~$0.3-0.6/position of unbooked cost can't slow-drift the wallet==book invariant (M2).
    # (ATA rent deposits are excluded — locked, recoverable value, not a cost.)
    fee_lamports: int = 0


class JupiterSwap:
    def __init__(self, *, rpc_url: str, slippage_bps: int = 300,
                 priority_level: str = "high", priority_max_lamports: int = 2_000_000,
                 timeout: float = 25.0):
        if not rpc_url:
            raise ValueError("rpc_url (SOLANA_RPC_URL) is required for live swaps")
        self.rpc_url = rpc_url
        self.slippage_bps = slippage_bps
        self.priority_level = priority_level
        self.priority_max_lamports = priority_max_lamports
        self._http = httpx.Client(timeout=timeout)   # httpx.Client is thread-safe for requests
        self._decimals_cache: dict[str, int] = {}
        self._cache_lock = threading.Lock()          # guards the decimals cache across workers

    # -- read-only: quote + build (safe to dry-run) ------------------------ #
    def quote(self, input_mint: str, output_mint: str, amount_raw: int,
              *, slippage_bps: Optional[int] = None) -> dict:
        r = self._http.get(f"{SWAP_BASE}/quote", params={
            "inputMint": input_mint, "outputMint": output_mint, "amount": int(amount_raw),
            # F08: per-call slippage override so buys can be tight and exits generous
            "slippageBps": int(slippage_bps if slippage_bps is not None else self.slippage_bps),
            "restrictIntermediateTokens": "true",
        })
        r.raise_for_status()
        return r.json()

    def build_swap(self, quote: dict, user_pubkey: str) -> str:
        r = self._http.post(f"{SWAP_BASE}/swap", json={
            "quoteResponse": quote, "userPublicKey": user_pubkey,
            "wrapAndUnwrapSol": True, "dynamicComputeUnitLimit": True,
            # F07: DYNAMIC priority fee (priority level with a hard lamports cap) instead of a
            # static 200k that fails to land in congestion — which would leave a -30% stop unfilled.
            "prioritizationFeeLamports": {
                "priorityLevelWithMaxLamports": {
                    "priorityLevel": self.priority_level,
                    "maxLamports": int(self.priority_max_lamports),
                }
            },
        })
        r.raise_for_status()
        return r.json()["swapTransaction"]

    # -- signing + sending (needs solders + a funded burner) --------------- #
    def execute_swap(self, swap_tx_b64: str, keypair, *, owner_pubkey: Optional[str] = None,
                     input_mint: Optional[str] = None, output_mint: Optional[str] = None) -> SwapResult:
        from solders.transaction import VersionedTransaction

        raw = base64.b64decode(swap_tx_b64)
        unsigned = VersionedTransaction.from_bytes(raw)
        signed = VersionedTransaction(unsigned.message, [keypair])
        wire = base64.b64encode(bytes(signed)).decode()
        sig = self._rpc("sendTransaction", [wire, {"encoding": "base64", "skipPreflight": False,
                                                   "maxRetries": 3}])
        confirmed = self._confirm(sig)
        in_amt = out_amt = fee = 0
        post_bal = -1
        if confirmed and owner_pubkey and input_mint and output_mint:
            try:
                in_amt, out_amt, post_bal, fee = self.landed_amounts_full(
                    sig, owner_pubkey, input_mint, output_mint)
            except Exception:
                pass    # confirmed but couldn't parse -> caller reconciles via token_balance
        return SwapResult(signature=sig, in_amount=in_amt, out_amount=out_amt,
                          confirmed=confirmed, post_balance=post_bal, fee_lamports=fee)

    def _rpc(self, method: str, params: list):
        try:
            r = self._http.post(self.rpc_url, json={"jsonrpc": "2.0", "id": 1,
                                                    "method": method, "params": params})
            r.raise_for_status()
        except httpx.HTTPStatusError as e:
            # SECURITY (audit #3): the exception str/repr embeds self.rpc_url (SOLANA_RPC_URL, which
            # carries the provider api-key in the path/subdomain/query). NEVER let it propagate — the
            # error is caught upstream as repr(e) and persisted to orders.note / logs. Status only.
            raise RuntimeError(f"RPC {method} HTTP {e.response.status_code}") from None
        except httpx.HTTPError as e:
            raise RuntimeError(f"RPC {method} transport error: {type(e).__name__}") from None
        body = r.json()
        if "error" in body:
            raise RuntimeError(f"RPC {method} error: {body['error']}")
        return body["result"]

    def _confirm(self, signature: str, tries: int = 90) -> bool:
        """Poll until confirmed/finalized. tries≈seconds — cover the FULL blockhash-validity window
        (~60-90s) so a slow-but-landed swap under congestion is NOT mis-classified as failed and then
        re-fired to book $0 on the flagship TP1 leg (audit re-verify #5). searchTransactionHistory=True
        so a tx that already landed is always found; a final sweep guards a transient error on the last poll."""
        import time
        for _ in range(tries):
            try:
                res = self._rpc("getSignatureStatuses",
                                [[signature], {"searchTransactionHistory": True}])
            except Exception:
                time.sleep(1.0)     # a transient RPC blip must NOT abandon the confirm — the tx may
                continue            # still land; keep polling (a genuinely lost tx times out below)
            st = (res.get("value") or [None])[0]
            if st and st.get("confirmationStatus") in ("confirmed", "finalized") and not st.get("err"):
                return True
            if st and st.get("err"):
                return False        # the tx landed but REVERTED — no point polling further
            time.sleep(1.0)
        # final sweep after the window (a landed tx is still retrievable by signature)
        try:
            res = self._rpc("getSignatureStatuses", [[signature], {"searchTransactionHistory": True}])
            st = (res.get("value") or [None])[0]
            if st and st.get("confirmationStatus") in ("confirmed", "finalized") and not st.get("err"):
                return True
        except Exception:
            pass
        return False

    # -- on-chain reads (the source of truth for sizing + reconciliation) --- #
    def token_decimals(self, mint: str) -> int:
        """Decimals from the IMMUTABLE mint account. FAIL CLOSED (raise) if unknown — never
        default to 6 for a real swap (F06). Cached (decimals never change)."""
        if mint == WSOL:
            return 9
        with self._cache_lock:
            hit = self._decimals_cache.get(mint)
        if hit is not None:
            return hit
        res = self._rpc("getAccountInfo", [mint, {"encoding": "jsonParsed"}])   # network, unlocked
        info = (((res or {}).get("value") or {}).get("data") or {}).get("parsed") or {}
        dec = (info.get("info") or {}).get("decimals")
        if not isinstance(dec, int):
            raise RuntimeError(f"could not read decimals for mint {mint} (fail-closed)")
        with self._cache_lock:
            self._decimals_cache[mint] = dec
        return dec

    def token_balance(self, owner_pubkey: str, mint: str) -> int:
        """Raw token amount the owner currently holds of `mint` (summed across token accounts).
        0 if none. The real remaining-bag figure that sizes sells + gates the idempotent buy."""
        return self.token_balance_ex(owner_pubkey, mint)[0]

    def token_balance_ex(self, owner_pubkey: str, mint: str) -> tuple[int, int]:
        """(raw_total, n_accounts). n_accounts=0 means the RPC node sees NO token account for
        this mint — on a node that hasn't indexed a fresh ATA yet that is indistinguishable from
        'never held', so a sell must treat total=0 WITH no account as AMBIGUOUS, never as proof a
        prior sell landed (the phantom-stop incident booked a $0 stop-out off exactly that misread).
        An account that EXISTS with a low/zero balance is a real answer."""
        res = self._rpc("getTokenAccountsByOwner",
                        [owner_pubkey, {"mint": mint}, {"encoding": "jsonParsed"}])
        vals = (res or {}).get("value") or []
        total = 0
        for acc in vals:
            amt = ((((acc.get("account") or {}).get("data") or {}).get("parsed") or {})
                   .get("info") or {}).get("tokenAmount") or {}
            try:
                total += int(amt.get("amount") or 0)
            except (TypeError, ValueError):
                continue
        return total, len(vals)

    def landed_amounts(self, signature: str, owner_pubkey: str,
                       input_mint: str, output_mint: str) -> tuple[int, int]:
        """ACTUAL (in_raw, out_raw) moved, parsed from the confirmed tx. Token sides come from
        pre/post token balances; a WSOL side (wrapped/unwrapped to SOL) comes from the owner's
        NET lamport delta — so a sell's real SOL proceeds (net of fees) are captured, not 0 (F4).
        Best-effort: returns 0 for a side it can't parse."""
        in_raw, out_raw, _ = self.landed_amounts_ex(signature, owner_pubkey,
                                                    input_mint, output_mint)
        return in_raw, out_raw

    def landed_amounts_ex(self, signature: str, owner_pubkey: str,
                          input_mint: str, output_mint: str) -> tuple[int, int, int]:
        """(in_raw, out_raw, token_post_balance_raw) — see landed_amounts_full."""
        in_raw, out_raw, post_bal, _fee = self.landed_amounts_full(
            signature, owner_pubkey, input_mint, output_mint)
        return in_raw, out_raw, post_bal

    def landed_amounts_full(self, signature: str, owner_pubkey: str,
                            input_mint: str, output_mint: str) -> tuple[int, int, int, int]:
        """(in_raw, out_raw, token_post_balance_raw, fee_lamports). The third value is the owner's
        balance of the TOKEN side (whichever of input/output is not WSOL) AFTER the tx — the
        authoritative wallet state at that slot, immune to read-lag on load-balanced RPC (the
        lagged-read class); -1 if it can't be parsed (0 is a real balance). fee_lamports is the tx
        fee actually paid (meta.fee — M2 cost booking).

        H1 (audit 2026-07-07): getTransaction defaults to FINALIZED while _confirm returns at
        CONFIRMED — without an explicit commitment the parse routinely returned null → (0,0,-1),
        proceeds booked from the QUOTE and the post-balance defense layer sat inert. Ask at
        "confirmed" and retry briefly (the tx can lag the status by a beat even at confirmed)."""
        import time
        tx = None
        for attempt in range(5):
            tx = self._rpc("getTransaction",
                           [signature, {"encoding": "jsonParsed", "commitment": "confirmed",
                                        "maxSupportedTransactionVersion": 0}])
            if tx is not None:
                break
            time.sleep(1.0 + attempt)   # 1,2,3,4s — ~10s total, well inside a leg's budget
        meta = (tx or {}).get("meta") or {}
        pre = meta.get("preTokenBalances") or []
        post = meta.get("postTokenBalances") or []

        def _held(balances: list, mint: str) -> int:
            tot = 0
            for b in balances:
                if b.get("owner") == owner_pubkey and b.get("mint") == mint:
                    try:
                        tot += int((b.get("uiTokenAmount") or {}).get("amount") or 0)
                    except (TypeError, ValueError):
                        continue
            return tot

        def _owner_lamport_gain() -> int:
            try:
                keys = (((tx.get("transaction") or {}).get("message") or {}).get("accountKeys") or [])
                idx = next((i for i, k in enumerate(keys)
                            if (k.get("pubkey") if isinstance(k, dict) else k) == owner_pubkey), None)
                if idx is None:
                    return 0
                gain = int((meta.get("postBalances") or [])[idx]) - int((meta.get("preBalances") or [])[idx])
                return max(0, gain)     # net SOL received (fees already netted out)
            except Exception:
                return 0

        out_raw = (_owner_lamport_gain() if output_mint == WSOL
                   else max(0, _held(post, output_mint) - _held(pre, output_mint)))
        in_raw = (0 if input_mint == WSOL
                  else max(0, _held(pre, input_mint) - _held(post, input_mint)))
        token_mint = output_mint if input_mint == WSOL else input_mint
        post_bal = _held(post, token_mint) if (tx or {}).get("meta") else -1
        try:
            fee = int(meta.get("fee") or 0)
        except (TypeError, ValueError):
            fee = 0
        return in_raw, out_raw, post_bal, fee


def load_burner_keypair():
    """Load the burner keypair from WALLET_PRIVATE_KEY (base58 or JSON byte array). Never logged.

    FAIL CLOSED (F09): the loaded key's pubkey MUST equal EXPECTED_BURNER_PUBKEY, else raise — the
    hard stop against ever signing with (or funding) any wallet but the sanctioned burner."""
    key = os.environ.get("WALLET_PRIVATE_KEY", "").strip()
    if not key:
        raise RuntimeError("WALLET_PRIVATE_KEY not set (BURNER ONLY)")
    from solders.keypair import Keypair

    if key.startswith("["):
        import json
        kp = Keypair.from_bytes(bytes(json.loads(key)))
    else:
        kp = Keypair.from_base58_string(key)
    if str(kp.pubkey()) != EXPECTED_BURNER_PUBKEY:
        # do NOT print the key or the loaded pubkey beyond the sanctioned constant
        raise RuntimeError("WALLET_PRIVATE_KEY is not the sanctioned burner "
                           f"({EXPECTED_BURNER_PUBKEY}) — refusing to arm")
    return kp
