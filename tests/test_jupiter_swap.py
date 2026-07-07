"""JupiterSwap on-chain parse invariants (H1): landed_amounts_ex must ask at the same
commitment _confirm returns at, and must retry a not-yet-visible tx instead of silently
booking (0,0,-1) — which left the whole post-balance defense layer inert in production."""

from __future__ import annotations

from memebot.live.jupiter_swap import WSOL, JupiterSwap

OWNER = "OwnerPubkey111111111111111111111111111111111"
MINT = "MintSwapTest11111111111111111111111111111111"


def _tx(post_tokens: int, lamport_gain: int = 0) -> dict:
    return {
        "transaction": {"message": {"accountKeys": [{"pubkey": OWNER}]}},
        "meta": {
            "preTokenBalances": [{"owner": OWNER, "mint": MINT,
                                  "uiTokenAmount": {"amount": str(post_tokens + 500)}}],
            "postTokenBalances": [{"owner": OWNER, "mint": MINT,
                                   "uiTokenAmount": {"amount": str(post_tokens)}}],
            "preBalances": [1_000_000_000],
            "postBalances": [1_000_000_000 + lamport_gain],
        },
    }


def _swap_with_rpc(responses: list, calls: list) -> JupiterSwap:
    sw = JupiterSwap(rpc_url="http://unit.test/rpc")

    def fake_rpc(method, params):
        calls.append((method, params))
        return responses.pop(0)

    sw._rpc = fake_rpc
    return sw


def test_landed_amounts_asks_at_confirmed_commitment(monkeypatch):
    calls: list = []
    sw = _swap_with_rpc([_tx(1000, lamport_gain=777)], calls)
    in_raw, out_raw, post = sw.landed_amounts_ex("sig", OWNER, MINT, WSOL)
    method, params = calls[0]
    assert method == "getTransaction"
    assert params[1]["commitment"] == "confirmed"
    assert (in_raw, out_raw, post) == (500, 777, 1000)


def test_landed_amounts_retries_a_not_yet_visible_tx(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    calls: list = []
    sw = _swap_with_rpc([None, None, _tx(42)], calls)
    _, _, post = sw.landed_amounts_ex("sig", OWNER, MINT, WSOL)
    assert post == 42
    assert len(calls) == 3


def test_landed_amounts_gives_up_cleanly_after_retries(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    calls: list = []
    sw = _swap_with_rpc([None] * 5, calls)
    in_raw, out_raw, post = sw.landed_amounts_ex("sig", OWNER, MINT, WSOL)
    assert (in_raw, out_raw, post) == (0, 0, -1)    # unknown, NOT a fake zero balance
    assert len(calls) == 5
