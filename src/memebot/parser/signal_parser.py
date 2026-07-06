"""Pure (no-network) parser for Telegram memecoin "call" messages.

This module turns the raw text of a Telegram message into a :class:`Signal`.
It is deliberately heuristic and best-effort: Telegram alpha channels have no
schema, so we extract what we can and attach confidence + notes explaining
every decision so downstream code (and humans) can audit it.

What we extract
---------------
* **Solana mint addresses** — base58 strings, 32-44 chars, drawn from the
  Solana/Bitcoin base58 alphabet (no ``0 O I l``). Launchpad mints commonly end
  in the literal suffix ``pump`` (pump.fun) or ``bonk`` (letsbonk.fun); when
  several candidates are present we prefer one of those as the primary mint.
* **Ticker** — the first ``$TICKER`` or ``#TICKER`` token (2-10 alphanumerics).
* **Side** — BUY / SELL / UPDATE / UNKNOWN inferred from keywords.
* **Numbers** — entry / target(s) / stop-loss, but only when explicitly labelled
  (``entry:``, ``target:``/``tp:``, ``sl:``/``stop:``). We never guess unlabelled
  numbers, because memecoin posts are full of market-cap/percentage noise.

Shape of typical inputs (observed from real channels)
-----------------------------------------------------
    "🚀 NEW CALL $WIF\nCA: EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm\n"
    "entry 0.0021 | target 0.01 0.05 | sl 0.0015"

    "Aping this one fr — 6p6xgHyF7AeE6TZkSmFsko444wqoP15icUSqi2jfGiPN pump"

    "Took profit, sold half my bag at 3x 🎉  $BONK still holding rest"

    "gm frens wagmi to the moon"        # noise -> UNKNOWN, confidence 0
"""

from __future__ import annotations

import re
from datetime import datetime

from memebot.models import Signal, SignalSide

# --------------------------------------------------------------------------- #
# Base58 mint extraction
# --------------------------------------------------------------------------- #
# Bitcoin/Solana base58 alphabet: digits 1-9 and letters minus 0 O I l.
_BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_BASE58_SET = frozenset(_BASE58_ALPHABET)
_MINT_MIN_LEN = 32
_MINT_MAX_LEN = 44

# Launchpad mint suffixes worth preferring as the primary mint.
_MINT_SUFFIXES = ("pump", "bonk")

# A "token" candidate: a run of base58-alphabet chars. We slice by a generous
# word boundary first (anything that is not a base58 char splits tokens), then
# validate length explicitly. Using the alphabet itself as the character class
# guarantees no out-of-alphabet char ever slips into a candidate.
_CANDIDATE_RE = re.compile(r"[1-9A-HJ-NP-Za-km-z]+")


def extract_solana_mints(text: str) -> list[str]:
    """Return base58 mint candidates found in ``text``.

    A candidate qualifies if it is 32-44 chars long and every character is in
    the base58 alphabet. Results are deduplicated preserving first-seen order,
    then reordered so that mints ending in a known launchpad suffix
    (``pump``/``bonk``) come first (still order-stable within each group).
    """
    if not text:
        return []

    seen: set[str] = set()
    suffixed: list[str] = []
    plain: list[str] = []

    for match in _CANDIDATE_RE.finditer(text):
        token = match.group(0)
        if not (_MINT_MIN_LEN <= len(token) <= _MINT_MAX_LEN):
            continue
        # Regex already restricts to the base58 alphabet, but re-validate
        # defensively so the contract (alphabet check) is explicit/local.
        if any(ch not in _BASE58_SET for ch in token):
            continue
        if token in seen:
            continue
        seen.add(token)
        if token.endswith(_MINT_SUFFIXES):
            suffixed.append(token)
        else:
            plain.append(token)

    return suffixed + plain


# --------------------------------------------------------------------------- #
# Ticker extraction
# --------------------------------------------------------------------------- #
# $TICKER or #TICKER, 2-10 alphanumerics. Must start with a letter so we don't
# grab "$100" or "#1". Case preserved.
_TICKER_RE = re.compile(r"[$#]([A-Za-z][A-Za-z0-9]{1,9})\b")


def _extract_ticker(text: str) -> str | None:
    m = _TICKER_RE.search(text)
    return m.group(1).upper() if m else None


# --------------------------------------------------------------------------- #
# Side classification
# --------------------------------------------------------------------------- #
# Order matters: a message can contain both buy- and sell-ish words ("sold half,
# still aping the rest"). We treat an explicit exit/update as taking precedence
# because acting on it as a fresh BUY would be wrong.
_SELL_UPDATE_RE = re.compile(
    r"\b(sold|selling|took profit|taking profit|take profit|tp\b|tp'?d|"
    r"closed|closing|exit(?:ed|ing)?|scal(?:e|ing) out|trim(?:med|ming)?|"
    r"realized|realised|secured|profit taken)\b",
    re.IGNORECASE,
)
_BUY_RE = re.compile(
    r"\b(buy(?:ing)?|aping|ape\b|aped|long(?:ing)?|entry|entered|entering|"
    r"calling|new call|new gem|fresh|loading|loaded|accumulat(?:e|ing)|"
    r"sending|send it|degen)\b",
    re.IGNORECASE,
)
_BULLISH_RE = re.compile(
    r"\b(moon|gem|100x|10x|2x|pump(?:ing|s)?|sending|sends|runner|"
    r"breakout|bullish|alpha|early|low cap|lowcap|mcap|launch(?:ed|ing)?)\b",
    re.IGNORECASE,
)


# --------------------------------------------------------------------------- #
# Labelled-number extraction
# --------------------------------------------------------------------------- #
_NUM = r"(\d+(?:\.\d+)?)"
# A number with optional k/m/b magnitude suffix (kept simple: we return the raw
# float; suffix handling is downstream's concern, we only capture the digits).
_ENTRY_RE = re.compile(r"\bentr(?:y|ies)\b\s*[:=]?\s*" + _NUM, re.IGNORECASE)
_STOP_RE = re.compile(r"\b(?:sl|stop(?:[\s-]?loss)?)\b\s*[:=]?\s*" + _NUM, re.IGNORECASE)
# Targets/TP may list several numbers: "target 0.01 0.05 0.1" or "tp: 2x 5x".
_TARGET_LABEL_RE = re.compile(
    r"\b(?:target|targets|tp|take[\s-]?profit)\b\s*[:=]?\s*", re.IGNORECASE
)
_NUM_RE = re.compile(_NUM)
# Number immediately followed by an 'x' is a multiple (e.g. "2x"); still a number.
_TRAILING_NUMS_RE = re.compile(r"\s*" + _NUM + r"x?\b")


def _first_float(rx: re.Pattern[str], text: str) -> float | None:
    m = rx.search(text)
    return float(m.group(1)) if m else None


def _extract_targets(text: str) -> list[float]:
    """Pull the run of numbers that follows a target/tp label."""
    m = _TARGET_LABEL_RE.search(text)
    if not m:
        return []
    rest = text[m.end():]
    targets: list[float] = []
    pos = 0
    # Consume consecutive "<number>[x]" tokens (allowing the small separators
    # commonly used: space, comma, pipe, slash, dash).
    while True:
        nm = _TRAILING_NUMS_RE.match(rest, pos)
        if not nm:
            break
        targets.append(float(nm.group(1)))
        pos = nm.end()
        sep = re.match(r"[\s,|/\-]+", rest[pos:])
        if sep:
            pos += sep.end()
        # If the next thing isn't a number, the run is over.
        if not _NUM_RE.match(rest, pos):
            break
    return targets


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #
def parse_message(
    source_channel: str,
    message_id: int,
    posted_at: datetime,
    text: str,
) -> Signal:
    """Parse one Telegram message into a :class:`Signal`.

    Confidence: 1.0 when an explicit mint is found, 0.5 when only a ticker is
    found, 0.0 when neither. ``parse_notes`` records the reasoning.
    """
    text = text or ""
    notes: list[str] = []

    mints = extract_solana_mints(text)
    ticker = _extract_ticker(text)

    # ----- mint -----
    mint: str | None = None
    if mints:
        mint = mints[0]
        if mint.endswith(_MINT_SUFFIXES):
            notes.append(f"mint has launchpad suffix '{mint[-4:]}' (preferred)")
        else:
            notes.append("mint extracted (no launchpad suffix)")
        if len(mints) > 1:
            notes.append(f"{len(mints)} mint candidates; using first")
    if ticker:
        notes.append(f"ticker ${ticker}")

    # ----- side -----
    sell_update = bool(_SELL_UPDATE_RE.search(text))
    buy = bool(_BUY_RE.search(text))
    bullish = bool(_BULLISH_RE.search(text))

    if sell_update:
        # "sold half / still holding" is an UPDATE; a clean exit is a SELL.
        if buy or re.search(r"\b(half|partial|some|rest|still)\b", text, re.IGNORECASE):
            side = SignalSide.UPDATE
            notes.append("exit + position language -> UPDATE")
        else:
            side = SignalSide.SELL
            notes.append("exit language -> SELL")
    elif buy:
        side = SignalSide.BUY
        notes.append("buy keyword -> BUY")
    elif mint is not None and bullish:
        side = SignalSide.BUY
        notes.append("mint + bullish context -> BUY")
    elif mint is not None:
        # A bare mint with no keywords is, by convention here, a call to BUY.
        side = SignalSide.BUY
        notes.append("bare mint, no keywords -> BUY (default)")
    else:
        side = SignalSide.UNKNOWN
        notes.append("no mint and no actionable keyword -> UNKNOWN")

    # ----- numbers (only when explicitly labelled) -----
    entry_hint = _first_float(_ENTRY_RE, text)
    if entry_hint is not None:
        notes.append(f"entry label -> {entry_hint}")
    stop_loss = _first_float(_STOP_RE, text)
    if stop_loss is not None:
        notes.append(f"stop label -> {stop_loss}")
    targets = _extract_targets(text)
    if targets:
        notes.append(f"target label -> {targets}")

    # ----- confidence -----
    if mint is not None:
        confidence = 1.0
    elif ticker is not None:
        confidence = 0.5
    else:
        confidence = 0.0
    notes.append(f"confidence {confidence}")

    return Signal(
        source_channel=source_channel,
        message_id=message_id,
        posted_at=posted_at,
        raw_text=text,
        side=side,
        mint=mint,
        ticker=ticker,
        entry_hint=entry_hint,
        targets=targets,
        stop_loss=stop_loss,
        parse_confidence=confidence,
        parse_notes=notes,
    )
