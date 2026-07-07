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
        self.no_account = set()     # mints with NO visible token account (RPC-lag ambiguity)
        self.balance_seq = {}       # mint -> [(raw, n_accounts), ...] served in order
        self.post_balance = -1      # SwapResult.post_balance (owner's post-tx token balance)
        self.landed = (0, 0)        # landed_amounts() result for late-landed recovery
        self.landed_ex = (0, 0, -1)  # landed_amounts_ex() result (in, out, post_balance)

    def quote(self, i, o, amt, *, slippage_bps=None):
        self.last_slippage = slippage_bps
        return {"outAmount": str(int(amt))}          # 1:1 raw by default

    def build_swap(self, q, pk):
        return "txb"

    def execute_swap(self, txb, kp, *, owner_pubkey, input_mint, output_mint):
        self.sent.append((input_mint, output_mint))
        return SwapResult("SIG", in_amount=0, out_amount=self.out_amount,
                          confirmed=self.confirm, post_balance=self.post_balance)

    def landed_amounts(self, signature, owner_pubkey, input_mint, output_mint):
        return self.landed

    def landed_amounts_ex(self, signature, owner_pubkey, input_mint, output_mint):
        return self.landed_ex

    def token_decimals(self, mint):
        if mint in self.raise_decimals:
            raise RuntimeError("fail-closed decimals")
        return self.decimals.get(mint, 6)

    def token_balance(self, owner, mint):
        return self.token_balance_ex(owner, mint)[0]

    def token_balance_ex(self, owner, mint):
        seq = self.balance_seq.get(mint)
        if seq:
            return seq.pop(0)
        if mint in self.no_account:
            return 0, 0
        return self.balances.get(mint, 0), 1


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
    # a full-bag stop sells what is actually held, however small
    fill = lx.sell_event(mint="M", stake_usd=3.0, entry_price=0.001,
                         event=_ev("STOP_OUT", frac=1.0, remaining_frac=0.0))
    assert fill.tokens == pytest.approx(1.0)
    st.close()


def test_partial_leg_without_sizing_key_fails_closed(tmp_path):
    """M4 (audit 2026-07-07): a 25% rung arriving without tokens_qty must raise, never fail
    open into dumping the whole bag on what should be a partial sell."""
    st, lx = _armed_live(tmp_path)
    lx._swap.decimals["M"] = 6
    lx._swap.balances["M"] = 1_000_000
    with pytest.raises(RuntimeError, match="missing tokens_qty"):
        lx.sell_event(mint="M", stake_usd=3.0, entry_price=0.001,
                      event=_ev("RIDE_SELL", frac=0.25, remaining_frac=0.75))
    assert lx._swap.sent == []                       # nothing fired
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


# -- phantom-stop incident (2026-07-07): an ambiguous zero-balance read fails CLOSED -------- #
def test_sell_with_no_token_account_raises_not_books_zero(tmp_path, monkeypatch):
    from memebot.live import executor as executor_mod
    monkeypatch.setattr(executor_mod.time, "sleep", lambda s: None)
    st, lx = _armed_live(tmp_path)
    lx._swap.decimals["M"] = 6
    lx._swap.no_account.add("M")              # lagging node: NO ATA visible for the mint
    with pytest.raises(RuntimeError, match="ambiguous zero-balance"):
        lx.sell_event(mint="M", stake_usd=3.0, entry_price=1.0,
                      event=_ev("STOP_OUT", frac=1.0, remaining_frac=0.0), tokens_qty=3.0)
    assert lx._swap.sent == []                # nothing sent — and NOTHING booked as a $0 fill
    st.close()


def test_sell_recovers_when_reread_finds_the_bag(tmp_path, monkeypatch):
    from memebot.live import executor as executor_mod
    monkeypatch.setattr(executor_mod.time, "sleep", lambda s: None)
    st, lx = _armed_live(tmp_path)
    lx._swap.decimals["M"] = 6
    lx._swap.balance_seq["M"] = [(0, 0), (3_000_000, 1)]   # first read lags; re-read sees the bag
    fill = lx.sell_event(mint="M", stake_usd=3.0, entry_price=1.0,
                         event=_ev("STOP_OUT", frac=1.0, remaining_frac=0.0), tokens_qty=3.0)
    assert fill.tokens == pytest.approx(3.0)  # the stop sells the REAL bag, not a phantom 0
    st.close()


def test_sell_zero_balance_with_visible_account_stays_idempotent(tmp_path):
    st, lx = _armed_live(tmp_path)
    lx._swap.decimals["M"] = 6
    lx._swap.balances["M"] = 0                # ATA exists, bag genuinely gone (a prior sell landed)
    fill = lx.sell_event(mint="M", stake_usd=3.0, entry_price=1.0,
                         event=_ev("FINALIZE", frac=1.0, remaining_frac=0.0), tokens_qty=3.0)
    assert fill.tokens == 0.0 and "idempotent" in fill.note
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


def test_sell_leg_never_oversells_past_its_modeled_size(tmp_path):
    """ladder-replay incident (2026-07-07): a STALE (too-high) balance read must not let one leg dump
    more than its own share — the leg clamps to tokens_qty * frac."""
    st, lx = _armed_live(tmp_path)
    lx._swap.decimals["M"] = 6
    lx._swap.balances["M"] = 12_000_000       # stale read: the prior leg's debit is not visible
    # this leg: frac 0.09375 of an original 24.0 -> 2.25 tokens max (target remaining 6.75)
    fill = lx.sell_event(mint="M", stake_usd=3.0, entry_price=0.001,
                         event=_ev("RIDE_SELL", frac=0.09375, remaining_frac=0.28125),
                         tokens_qty=24.0)
    assert fill.tokens == pytest.approx(2.25)  # NOT held-target = 12.0 - 6.75 = 5.25
    st.close()


# -- on-chain truth memory: the confirmed tx's post-balance beats any RPC read ---------- #
def test_sell_uses_confirmed_post_balance_over_stale_read(tmp_path):
    st, lx = _armed_live(tmp_path)
    lx._swap.decimals["M"] = 6
    lx._swap.balances["M"] = 12_000_000       # STALE read (prior leg's debit not visible)
    lx._post_bal["M"] = 9_000_000             # truth from OUR last confirmed tx
    fill = lx.sell_event(mint="M", stake_usd=3.0, entry_price=0.001,
                         event=_ev("RIDE_SELL", frac=0.5, remaining_frac=0.5),
                         tokens_qty=12.0)
    assert fill.tokens == pytest.approx(3.0)  # sized from 9.0 held, NOT the stale 12.0
    st.close()


def test_sell_no_account_read_falls_back_to_cache(tmp_path):
    st, lx = _armed_live(tmp_path)
    lx._swap.decimals["M"] = 6
    lx._swap.no_account.add("M")              # lagging node: no ATA visible at all
    lx._post_bal["M"] = 3_000_000             # but our last confirmed tx says we hold 3.0
    fill = lx.sell_event(mint="M", stake_usd=3.0, entry_price=1.0,
                         event=_ev("STOP_OUT", frac=1.0, remaining_frac=0.0), tokens_qty=3.0)
    assert fill.tokens == pytest.approx(3.0)  # the stop exits — no raise, no stuck retry loop
    st.close()


def test_confirmed_sell_updates_cache_and_clean_leg_passes_verify(tmp_path):
    st, lx = _armed_live(tmp_path)
    lx._swap.decimals["M"] = 6
    lx._swap.balances["M"] = 1_000_000
    lx._swap.post_balance = 670_000           # tx post-balance == the leg's target
    lx.sell_event(mint="M", stake_usd=3.0, entry_price=1.0,
                  event=_ev("TP", frac=0.33, remaining_frac=0.67), tokens_qty=1.0)
    assert lx._post_bal["M"] == 670_000
    assert st.get_system("kill_switch") != "on"
    st.close()


def test_leg_drift_trips_kill_switch(tmp_path):
    st, lx = _armed_live(tmp_path)
    lx._swap.decimals["M"] = 6
    lx._swap.balances["M"] = 1_000_000
    lx._swap.post_balance = 100_000           # wallet ended FAR below the leg's 670_000 target
    fill = lx.sell_event(mint="M", stake_usd=3.0, entry_price=1.0,
                         event=_ev("TP", frac=0.33, remaining_frac=0.67), tokens_qty=1.0)
    assert fill.usd > 0                       # the landed leg still books
    assert st.get_system("kill_switch") == "on"    # ...but buys halt + CRIT raised
    kinds = [a["kind"] for a in st.query("SELECT kind FROM alerts")]
    assert any(k.startswith("LEG_DRIFT_") for k in kinds)
    st.close()


def test_late_landed_sell_proceeds_recovered_not_zero(tmp_path):
    st, lx = _armed_live(tmp_path)
    lx._swap.decimals["M"] = 6
    lx._swap.balances["M"] = 1_000_000
    lx._swap.confirm = False                  # confirm times out; the tx will land LATE
    with pytest.raises(SwapNotConfirmed):
        lx.sell_event(mint="M", stake_usd=3.0, entry_price=1.0,
                      event=_ev("TP", frac=0.33, remaining_frac=0.67), tokens_qty=1.0)
    lx._swap.confirm = True
    lx._swap.balances["M"] = 670_000          # the late tx landed: balance already at target
    lx._swap.landed_ex = (330_000, 5_000_000, 670_000)   # 0.33 tokens sold for 0.005 SOL
    fill = lx.sell_event(mint="M", stake_usd=3.0, entry_price=1.0,
                         event=_ev("TP", frac=0.33, remaining_frac=0.67), tokens_qty=1.0)
    assert "recovered" in fill.note           # NOT a $0 idempotent skip
    assert fill.usd == pytest.approx(1.0)     # 0.005 SOL x $200 — the REAL proceeds
    assert fill.tokens == pytest.approx(0.33)
    st.close()


# -- AUDIT B1/B2 (2026-07-07): post-confirm is fail-safe; recovery folds into ANY next leg ----- #
def test_recovered_proceeds_fold_into_a_nonzero_next_leg(tmp_path):
    """A confirm-timeout TP1 that lands late must have its cash folded into the NEXT leg's fill
    even when that leg is a nonzero stop — not only into a $0 idempotent skip."""
    st, lx = _armed_live(tmp_path)
    lx._swap.decimals["M"] = 6
    lx._swap.balances["M"] = 1_000_000
    lx._swap.confirm = False                       # TP1 send times out...
    with pytest.raises(SwapNotConfirmed):
        lx.sell_event(mint="M", stake_usd=3.0, entry_price=1.0,
                      event=_ev("TP", frac=0.33, remaining_frac=0.67), tokens_qty=1.0)
    lx._swap.confirm = True
    lx._swap.balances["M"] = 670_000               # ...but landed late: 0.33 sold
    lx._swap.landed_ex = (330_000, 5_000_000, 670_000)   # its real proceeds: 0.005 SOL = $1.00
    # price dumped -> the rolled-back rider fires a FULL STOP (nonzero amount: 0.67 tokens)
    fill = lx.sell_event(mint="M", stake_usd=3.0, entry_price=1.0,
                         event=_ev("STOP_OUT", frac=1.0, remaining_frac=0.0), tokens_qty=1.0)
    assert "recovered" in fill.note
    assert fill.tokens == pytest.approx(0.67 + 0.33)
    # stop proceeds (670_000 raw 1:1 -> 0.00067 SOL x $200 = $0.134) + the recovered $1.00
    assert fill.usd == pytest.approx(0.134 + 1.0)
    assert lx._unconfirmed_sell == {}              # sig resolved exactly once
    st.close()


def test_post_confirm_price_failure_still_books_the_fill(tmp_path):
    """AUDIT B1: nothing may raise after the swap confirmed — a Jupiter price-API outage during
    the post-confirm USD conversion must not turn a LANDED sell into a 'failed' leg."""
    st, lx = _armed_live(tmp_path)

    class RaisingJC:
        def price(self, mints):
            raise RuntimeError("price API down")

    lx.jc = RaisingJC()
    lx._last_sol_usd = 150.0                       # a prior leg saw a good price
    lx._swap.decimals["M"] = 6
    lx._swap.balances["M"] = 1_000_000
    fill = lx.sell_event(mint="M", stake_usd=3.0, entry_price=1.0,
                         event=_ev("STOP_OUT", frac=1.0, remaining_frac=0.0), tokens_qty=1.0)
    assert fill.usd == pytest.approx((1_000_000 / 1e9) * 150.0)   # last-good price, no raise
    st.close()


# -- H2 (audit 2026-07-07): the double-buy mirror of the $0-sell incident ------------ #
def test_buy_adopts_late_landed_buy_instead_of_double_buying(tmp_path):
    """A buy whose confirm timed out but LANDED must be adopted from the tx itself on the
    next ENTER — never bought a second time off a lag-prone balance read."""
    st, lx = _armed_live(tmp_path)
    lx._swap.decimals["M"] = 6
    lx._swap.confirm = False
    with pytest.raises(SwapNotConfirmed):
        lx.buy(mint="M", stake_usd=3.0, entry_price=1.0)
    assert lx._unconfirmed_buy["M"] == "SIG"

    lx._swap.confirm = True
    lx._swap.landed_ex = (0, 5_000_000, 5_000_000)   # the timed-out buy landed: 5.0 tokens
    lx._swap.no_account.add("M")                     # and the balance read STILL lags
    fill = lx.buy(mint="M", stake_usd=3.0, entry_price=1.0)
    assert fill.tokens == pytest.approx(5.0)
    assert "late-landed" in fill.note
    assert len(lx._swap.sent) == 1                   # ONE send total — no second buy
    assert lx._post_bal["M"] == 5_000_000            # on-chain truth cached for the next sell
    st.close()


def test_buy_adopts_cached_post_balance_over_a_lagged_zero_read(tmp_path):
    """Our own confirmed buy's post-balance beats a node that has not indexed the ATA yet."""
    st, lx = _armed_live(tmp_path)
    lx._swap.decimals["M"] = 6
    lx._post_bal["M"] = 3_000_000                    # confirmed on-chain truth from a prior buy
    lx._swap.no_account.add("M")                     # fresh read sees nothing (RPC lag)
    fill = lx.buy(mint="M", stake_usd=3.0, entry_price=1.0)
    assert fill.tokens == pytest.approx(3.0)
    assert "adopted" in fill.note
    assert lx._swap.sent == []                       # no swap fired
    st.close()


# -- re-audit (2026-07-07 evening): the remaining $0-sell doors ---------------------- #
def test_visible_empty_ata_with_healthy_cache_never_books_zero(tmp_path, monkeypatch):
    """A token account VISIBLE at 0 on a lagged node (ATAs are never closed by a full sell)
    while our own confirmed buy's post-balance says 24k tokens: min() must not discard the
    cache and book a $0 stop — the disagreement is ambiguous, and a second agreeing read is
    required either way."""
    from memebot.live import executor as executor_mod
    monkeypatch.setattr(executor_mod.time, "sleep", lambda s: None)
    st, lx = _armed_live(tmp_path)
    lx._swap.decimals["M"] = 6
    lx._post_bal["M"] = 24_000_000                    # authoritative: we HOLD 24 tokens
    lx._swap.balance_seq["M"] = [(0, 1), (0, 1)]      # lagged node: visible account, balance 0
    with pytest.raises(RuntimeError, match="ambiguous"):
        lx.sell_event(mint="M", stake_usd=3.0, entry_price=1.0,
                      event=_ev("STOP_OUT", frac=1.0, remaining_frac=0.0), tokens_qty=24.0)
    assert lx._swap.sent == []
    st.close()


def test_disagreeing_stale_cache_is_overruled_by_two_fresh_reads(tmp_path, monkeypatch):
    """Variant (b): a 0 cache that outlived a close vs a real 24-token wallet (manual re-buy).
    Two agreeing fresh reads beat the disagreeing cache — the sell proceeds, sized correctly."""
    from memebot.live import executor as executor_mod
    monkeypatch.setattr(executor_mod.time, "sleep", lambda s: None)
    st, lx = _armed_live(tmp_path)
    lx._swap.decimals["M"] = 6
    lx._post_bal["M"] = 0                             # stale: outlived the closed position
    lx._swap.balances["M"] = 24_000_000               # the wallet really holds 24 tokens
    fill = lx.sell_event(mint="M", stake_usd=3.0, entry_price=1.0,
                         event=_ev("STOP_OUT", frac=1.0, remaining_frac=0.0), tokens_qty=24.0)
    assert fill.tokens == pytest.approx(24.0)         # full stop actually fired
    st.close()


def test_unresolved_prior_buy_blocks_a_second_buy(tmp_path):
    """Re-audit: when the prior unconfirmed buy's fate is UNKNOWN (tx read fails), a new buy
    must not be sent — fail closed, keep the sig, retry next tick."""
    st, lx = _armed_live(tmp_path)
    lx._swap.decimals["M"] = 6
    lx._unconfirmed_buy["M"] = "OLD_SIG"

    def boom(*a, **kw):
        raise RuntimeError("rpc down")
    lx._swap.landed_amounts_ex = boom
    with pytest.raises(RuntimeError, match="double buy"):
        lx.buy(mint="M", stake_usd=3.0, entry_price=1.0)
    assert lx._unconfirmed_buy["M"] == "OLD_SIG"      # stash preserved
    assert lx._swap.sent == []
    st.close()


def test_recovery_stash_survives_a_failed_next_leg(tmp_path):
    """Re-audit: recovered proceeds are only committed when a fill RETURNS — a next leg that
    itself fails (quote error) must leave the landed prior sig stashed for the leg after."""
    st, lx = _armed_live(tmp_path)
    lx._swap.decimals["M"] = 6
    lx._swap.balances["M"] = 670_000
    lx._unconfirmed_sell["M"] = {"LATE_SIG": 0}
    lx._swap.landed_ex = (330_000, 5_000_000, 670_000)   # the late TP landed: $1.00 real

    def bad_quote(*a, **kw):
        raise RuntimeError("quote api down")
    lx._swap.quote = bad_quote
    with pytest.raises(RuntimeError, match="quote api down"):
        lx.sell_event(mint="M", stake_usd=3.0, entry_price=1.0,
                      event=_ev("STOP_OUT", frac=1.0, remaining_frac=0.0), tokens_qty=1.0)
    assert "LATE_SIG" in lx._unconfirmed_sell["M"]    # NOT consumed by the failed leg
    st.close()
