"""LiveExecutor safety tests (phase A) — confirm-then-commit, real-balance sizing, kill-switch
buys-only, fail-closed decimals, the failure breaker, and the operator gates. All offline via a
fake swap client (no solders, no RPC) — the path stays INERT; these prove it is CORRECT when armed."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from memebot.live.executor import LiveExecutor, SwapNotConfirmed, SWAP_FAILURE_BREAKER
from memebot.live.jupiter_swap import SwapResult, WSOL
from memebot.live.state import LiveState
from memebot.live.strategy import Event, TailRiderConfig

T0 = datetime(2026, 6, 1, tzinfo=timezone.utc)


class FakeKeypair:
    def pubkey(self):
        return "BURNER"


class FakeJC:
    """JupiterClient stand-in for _sol_usd: SOL = $200."""
    def price(self, mints):
        return {WSOL: 200.0}


class FakeSwap:
    def __init__(self):
        self.decimals = {}          # mint -> decimals
        self.balances = {}          # mint -> raw held
        self.confirm = True
        self.out_amount = 0         # SwapResult.out_amount returned by execute_swap
        self.sent = []              # (input_mint, output_mint) per execute_swap
        self.last_slippage = None
        self.raise_decimals = set()

    def quote(self, i, o, amt, *, slippage_bps=None):
        self.last_slippage = slippage_bps
        return {"outAmount": str(int(amt))}          # 1:1 raw by default

    def build_swap(self, q, pk):
        return "txb"

    def execute_swap(self, txb, kp, *, owner_pubkey, input_mint, output_mint):
        self.sent.append((input_mint, output_mint))
        return SwapResult("SIG", in_amount=0, out_amount=self.out_amount, confirmed=self.confirm)

    def token_decimals(self, mint):
        if mint in self.raise_decimals:
            raise RuntimeError("fail-closed decimals")
        return self.decimals.get(mint, 6)

    def token_balance(self, owner, mint):
        return self.balances.get(mint, 0)


def _armed_live(tmp_path, *, dry_run=False, gates=True):
    st = LiveState(tmp_path / "s.db")
    st.set_system("mode", "live")
    st.set_system("kill_switch", "off")
    if gates:                                        # F11 operator gates for real sends
        st.set_system("equivalence_ok", "1")
        st.set_system("dust_reconciled", "1")
    lx = LiveExecutor(st, FakeJC(), TailRiderConfig(), armed=True, dry_run=dry_run)
    lx._swap = FakeSwap()
    lx._keypair = FakeKeypair()                      # inject -> _ensure_clients skips load_burner_keypair
    return st, lx


def _ev(kind, **kw):
    return Event(ts=T0, kind=kind, **kw)


# -- F01: confirm-then-commit ------------------------------------------------- #
def test_buy_not_confirmed_raises_and_increments_breaker(tmp_path):
    st, lx = _armed_live(tmp_path)
    lx._swap.confirm = False
    with pytest.raises(SwapNotConfirmed):
        lx.buy(mint="M", stake_usd=3.0, entry_price=1.0)
    assert st.get_system("consecutive_swap_failures") == "1"
    st.close()


def test_buy_confirmed_uses_onchain_out_amount_not_quote(tmp_path):
    st, lx = _armed_live(tmp_path)
    lx._swap.decimals["M"] = 6
    lx._swap.out_amount = 2_000_000                  # ACTUAL 2.0 tokens received (6 decimals)
    fill = lx.buy(mint="M", stake_usd=3.0, entry_price=1.0)
    assert fill.tokens == pytest.approx(2.0)         # from res.out_amount, not the quote's expectation
    assert st.get_system("consecutive_swap_failures") in (None, "0")
    st.close()


def test_sell_not_confirmed_raises(tmp_path):
    st, lx = _armed_live(tmp_path)
    lx._swap.decimals["M"] = 6
    lx._swap.balances["M"] = 1_000_000
    lx._swap.confirm = False
    with pytest.raises(SwapNotConfirmed):
        lx.sell_event(mint="M", stake_usd=3.0, entry_price=1.0, event=_ev("STOP_OUT", frac=1.0))
    st.close()


# -- F54: idempotent entry ---------------------------------------------------- #
def test_buy_adopts_existing_balance_without_a_second_send(tmp_path):
    st, lx = _armed_live(tmp_path)
    lx._swap.decimals["M"] = 6
    lx._swap.balances["M"] = 5_000_000               # already hold 5.0 tokens (crash before DB commit)
    fill = lx.buy(mint="M", stake_usd=3.0, entry_price=1.0)
    assert fill.tokens == pytest.approx(5.0) and "adopted" in fill.note
    assert lx._swap.sent == []                       # NO second swap sent
    st.close()


# -- F02: sell sizes from the real wallet ------------------------------------- #
def test_sell_clamps_to_held_balance(tmp_path):
    st, lx = _armed_live(tmp_path)
    lx._swap.decimals["M"] = 6
    lx._swap.balances["M"] = 1_000_000               # hold only 1.0 token
    # modeled wants 0.25 * (3/0.001) = 750 tokens -> must clamp to the 1.0 actually held
    fill = lx.sell_event(mint="M", stake_usd=3.0, entry_price=0.001,
                         event=_ev("RIDE_SELL", frac=0.25, remaining_frac=0.75))
    assert fill.tokens == pytest.approx(1.0)
    st.close()


def test_sell_sizes_down_to_target_remaining(tmp_path):
    st, lx = _armed_live(tmp_path)
    lx._swap.decimals["M"] = 6
    lx._swap.balances["M"] = 1_000_000        # hold the full 1.0 token
    # TP1: remaining_frac 0.67 of a 1.0-token entry -> sell down to 670_000 -> sell 330_000
    fill = lx.sell_event(mint="M", stake_usd=3.0, entry_price=1.0,
                         event=_ev("TP", frac=0.33, remaining_frac=0.67), tokens_qty=1.0)
    assert fill.tokens == pytest.approx(0.33)
    st.close()


def test_sell_is_idempotent_when_already_at_target(tmp_path):
    st, lx = _armed_live(tmp_path)
    lx._swap.decimals["M"] = 6
    lx._swap.balances["M"] = 670_000          # already sold down to the 33%-TP target
    fill = lx.sell_event(mint="M", stake_usd=3.0, entry_price=1.0,
                         event=_ev("TP", frac=0.33, remaining_frac=0.67), tokens_qty=1.0)
    assert fill.tokens == 0.0 and "idempotent" in fill.note   # re-fire is a NO-OP — no double-sell
    assert lx._swap.sent == []
    st.close()


def test_landed_amounts_parses_real_sol_received_on_sell():
    from memebot.live.jupiter_swap import JupiterSwap
    js = JupiterSwap.__new__(JupiterSwap)     # bypass __init__ (no rpc_url / httpx needed)
    tx = {"transaction": {"message": {"accountKeys": [{"pubkey": "OWNER"}, {"pubkey": "X"}]}},
          "meta": {"preBalances": [1000, 0], "postBalances": [1500, 0],
                   "preTokenBalances": [{"owner": "OWNER", "mint": "TOK",
                                         "uiTokenAmount": {"amount": "100"}}],
                   "postTokenBalances": [{"owner": "OWNER", "mint": "TOK",
                                          "uiTokenAmount": {"amount": "0"}}]}}
    js._rpc = lambda method, params: tx
    in_raw, out_raw = js.landed_amounts("sig", "OWNER", input_mint="TOK", output_mint=WSOL)
    assert in_raw == 100         # tokens sold (from token-balance delta)
    assert out_raw == 500        # REAL net SOL received (owner lamport delta), not 0 (F4)


def test_sell_finalize_dumps_entire_balance(tmp_path):
    st, lx = _armed_live(tmp_path)
    lx._swap.decimals["M"] = 6
    lx._swap.balances["M"] = 3_333_333               # dust-y remainder
    fill = lx.sell_event(mint="M", stake_usd=3.0, entry_price=1.0,
                         event=_ev("FINALIZE", frac=0.42))
    assert fill.tokens == pytest.approx(3_333_333 / 1e6)   # whole bag, not the modeled fraction
    st.close()


# -- F04: kill-switch blocks buys only ---------------------------------------- #
def test_killswitch_blocks_buy_but_never_the_stop_sell(tmp_path):
    st, lx = _armed_live(tmp_path)
    lx._swap.decimals["M"] = 6
    lx._swap.balances["M"] = 1_000_000
    st.set_system("kill_switch", "on")
    with pytest.raises(PermissionError):
        lx.buy(mint="M", stake_usd=3.0, entry_price=1.0)
    # a STOP_OUT sell STILL executes — you must be able to cut losers while the kill is tripped
    fill = lx.sell_event(mint="M", stake_usd=3.0, entry_price=1.0, event=_ev("STOP_OUT", frac=1.0))
    assert fill.kind == "SELL"
    st.close()


# -- F06: decimals fail closed ------------------------------------------------ #
def test_decimals_fail_closed_never_defaults_to_6(tmp_path):
    st, lx = _armed_live(tmp_path)
    lx._swap.raise_decimals.add("M")
    with pytest.raises(RuntimeError):
        lx.buy(mint="M", stake_usd=3.0, entry_price=1.0)
    st.close()


# -- F08: per-leg slippage ---------------------------------------------------- #
def test_exit_slippage_is_wider_than_entry(tmp_path):
    st, lx = _armed_live(tmp_path)
    lx._swap.decimals["M"] = 6
    lx._swap.out_amount = 1_000_000
    lx.buy(mint="M", stake_usd=3.0, entry_price=1.0)
    assert lx._swap.last_slippage == lx.entry_slippage_bps          # tight entry
    lx._swap.balances["M"] = 1_000_000
    lx.sell_event(mint="M", stake_usd=3.0, entry_price=1.0, event=_ev("STOP_OUT", frac=1.0))
    assert lx._swap.last_slippage == lx.exit_slippage_bps           # wide exit
    assert lx.exit_slippage_bps > lx.entry_slippage_bps
    st.close()


# -- F11: failure breaker + operator gates ------------------------------------ #
def test_breaker_trips_kill_after_n_consecutive_failures(tmp_path):
    st, lx = _armed_live(tmp_path)
    lx._swap.confirm = False
    for _ in range(SWAP_FAILURE_BREAKER):
        with pytest.raises(SwapNotConfirmed):
            lx.buy(mint="M", stake_usd=3.0, entry_price=1.0)
    assert st.get_system("kill_switch") == "on"
    assert "SWAP_BREAKER" in [a["kind"] for a in st.recent_alerts()]
    st.close()


def test_real_send_requires_operator_gates(tmp_path):
    st, lx = _armed_live(tmp_path, gates=False)       # equivalence_ok / dust_reconciled NOT set
    lx._swap.decimals["M"] = 6
    with pytest.raises(PermissionError):
        lx.buy(mint="M", stake_usd=3.0, entry_price=1.0)
    st.close()


def test_success_resets_the_breaker_counter(tmp_path):
    st, lx = _armed_live(tmp_path)
    st.set_system("consecutive_swap_failures", "2")
    lx._swap.decimals["M"] = 6
    lx._swap.out_amount = 1_000_000
    lx.buy(mint="M", stake_usd=3.0, entry_price=1.0)
    assert st.get_system("consecutive_swap_failures") == "0"
    st.close()


# -- F09: burner allowlist ---------------------------------------------------- #
def test_burner_keypair_requires_env(monkeypatch):
    # the env guard is testable without solders (checked before the lazy import)
    monkeypatch.delenv("WALLET_PRIVATE_KEY", raising=False)
    from memebot.live.jupiter_swap import load_burner_keypair
    with pytest.raises(RuntimeError):
        load_burner_keypair()


def test_burner_allowlist_rejects_unsanctioned_key(monkeypatch):
    # audit #11: THE hardest real-money stop — load_burner_keypair MUST raise for ANY key whose pubkey
    # is not the sanctioned burner. Previously only the missing-env branch was covered; a broken/
    # inverted allowlist would ship green while signing with a funded, unsanctioned wallet.
    import json
    pytest.importorskip("solders")
    from solders.keypair import Keypair
    from memebot.live.jupiter_swap import load_burner_keypair
    wrong = Keypair()                                            # a valid but WRONG key
    monkeypatch.setenv("WALLET_PRIVATE_KEY", json.dumps(list(bytes(wrong))))
    with pytest.raises(RuntimeError):
        load_burner_keypair()


def test_burner_allowlist_accepts_the_sanctioned_key(monkeypatch):
    # cover the MATCH branch: with EXPECTED_BURNER_PUBKEY pointed at a fresh key, its own key loads.
    import json
    pytest.importorskip("solders")
    from solders.keypair import Keypair
    from memebot.live import jupiter_swap
    kp = Keypair()
    monkeypatch.setattr(jupiter_swap, "EXPECTED_BURNER_PUBKEY", str(kp.pubkey()))
    monkeypatch.setenv("WALLET_PRIVATE_KEY", json.dumps(list(bytes(kp))))
    loaded = jupiter_swap.load_burner_keypair()
    assert str(loaded.pubkey()) == str(kp.pubkey())
