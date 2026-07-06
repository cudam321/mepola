"""Tests for the pure Telegram signal parser. No network."""

from __future__ import annotations

from datetime import datetime, timezone

from memebot.models import SignalSide
from memebot.parser.signal_parser import extract_solana_mints, parse_message

# Fixed tz-aware UTC timestamp used by every parse_message call.
TS = datetime(2026, 6, 21, 12, 0, 0, tzinfo=timezone.utc)

# A real-shaped pump.fun mint: 44 base58 chars ending in "pump".
PUMP_MINT = "6p6xgHyF7AeE6TZkSmFsko444wqoP15icUSqi2jGpump"


def _parse(text: str):
    return parse_message("test_channel", 1, TS, text)


# 1) Real-looking pump mint call -> mint extracted, side BUY, confidence 1.0.
def test_pump_mint_call_is_buy_confidence_1():
    assert 32 <= len(PUMP_MINT) <= 44
    text = f"🚀 NEW CALL aping this fr\nCA: {PUMP_MINT}\nlow cap gem to the moon"
    sig = _parse(text)
    assert sig.mint == PUMP_MINT
    assert sig.side == SignalSide.BUY
    assert sig.parse_confidence == 1.0
    assert sig.posted_at == TS


def test_pump_mint_preferred_over_plain_candidate():
    plain = "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm"  # 44 chars, no suffix
    assert 32 <= len(plain) <= 44
    # Plain candidate appears first in the text, but the pump one wins.
    sig = _parse(f"two addrs {plain} and {PUMP_MINT} pick the pump one")
    assert sig.mint == PUMP_MINT
    assert plain in extract_solana_mints(sig.raw_text)


# 2) $TICKER-only call -> ticker set, mint None, confidence 0.5.
def test_ticker_only_call():
    sig = _parse("New call $WIF looks bullish, early gem, aping soon")
    assert sig.ticker == "WIF"
    assert sig.mint is None
    assert sig.parse_confidence == 0.5
    assert sig.side == SignalSide.BUY


def test_hashtag_ticker_also_works():
    sig = _parse("watching #BONK closely")
    assert sig.ticker == "BONK"
    assert sig.mint is None
    assert sig.parse_confidence == 0.5


# 3) "took profit, sold half" update -> side UPDATE or SELL.
def test_took_profit_sold_half_is_update_or_sell():
    sig = _parse("Took profit, sold half my bag at 3x 🎉 still holding the rest")
    assert sig.side in (SignalSide.UPDATE, SignalSide.SELL)


def test_clean_exit_is_sell():
    sig = _parse("Closed the position, fully sold. Done with this one.")
    assert sig.side in (SignalSide.SELL, SignalSide.UPDATE)


# 4) Noise message -> UNKNOWN / confidence 0.
def test_noise_message_is_unknown():
    sig = _parse("gm frens wagmi lfg to the moon")
    assert sig.side == SignalSide.UNKNOWN
    assert sig.mint is None
    assert sig.ticker is None
    assert sig.parse_confidence == 0.0


# 5) extract_solana_mints rejects too-short and non-base58 strings.
def test_extract_rejects_short_and_non_base58():
    assert extract_solana_mints("short abc123 nope") == []
    # 0, O, I, l are not in the base58 alphabet -> a 40-char string with them is rejected.
    bad = "0OIl" + "1" * 38  # 42 chars but contains forbidden chars at the start
    assert bad not in extract_solana_mints(f"bad addr {bad} here")
    # A 31-char all-base58 string is too short.
    too_short = "1" * 31
    assert extract_solana_mints(too_short) == []
    # A valid 43-char base58 string is accepted.
    ok = "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcj"
    assert len(ok) == 43
    assert extract_solana_mints(f"addr {ok}") == [ok]


def test_extract_dedupes_preserving_order():
    a = "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm"
    out = extract_solana_mints(f"{a} repeated {a} again")
    assert out == [a]


def test_extract_orders_suffixed_first():
    plain = "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm"
    out = extract_solana_mints(f"{plain} then {PUMP_MINT}")
    assert out[0] == PUMP_MINT
    assert plain in out


# Bonus: labelled numeric extraction.
def test_labelled_numbers_extracted():
    text = (
        f"CA {PUMP_MINT}\n"
        "entry: 0.0021 | target 0.01 0.05 0.1 | sl 0.0015"
    )
    sig = _parse(text)
    assert sig.entry_hint == 0.0021
    assert sig.stop_loss == 0.0015
    assert sig.targets == [0.01, 0.05, 0.1]


def test_unlabelled_numbers_ignored():
    # Market cap / percentages must not be misread as entry/target/stop.
    sig = _parse(f"CA {PUMP_MINT} mcap 45k up 300% holders 1200")
    assert sig.entry_hint is None
    assert sig.stop_loss is None
    assert sig.targets == []
