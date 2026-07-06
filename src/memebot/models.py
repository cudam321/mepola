"""Core data models — the contract shared by every module.

All timestamps are timezone-aware UTC ``datetime`` objects. All prices are USD per
token unless a field name says otherwise. Multiples are linear (1.0 = break-even,
2.0 = +100%, 0.0 = total loss).
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from typing import Any, Optional


# --------------------------------------------------------------------------- #
# Signals (parsed Telegram calls)
# --------------------------------------------------------------------------- #
class SignalSide(str, Enum):
    BUY = "buy"
    SELL = "sell"
    UPDATE = "update"   # follow-up like "sold half", "taking profit"
    UNKNOWN = "unknown"


@dataclass
class Signal:
    """One parsed call from a channel. ``mint`` is the canonical key for trading;
    ``ticker`` alone is ambiguous and must be resolved to a mint before use."""
    source_channel: str
    message_id: int
    posted_at: datetime                    # UTC, when the message was posted
    raw_text: str
    chain: str = "solana"
    side: SignalSide = SignalSide.BUY
    mint: Optional[str] = None             # resolved/extracted token mint address
    ticker: Optional[str] = None
    # Optional structured fields if the channel provides them:
    entry_hint: Optional[float] = None
    targets: list[float] = field(default_factory=list)
    stop_loss: Optional[float] = None
    # Parser metadata:
    parse_confidence: float = 0.0          # 0..1
    parse_notes: list[str] = field(default_factory=list)

    @property
    def is_tradable(self) -> bool:
        return self.side == SignalSide.BUY and self.mint is not None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["posted_at"] = self.posted_at.isoformat()
        d["side"] = self.side.value
        return d


# --------------------------------------------------------------------------- #
# Market data
# --------------------------------------------------------------------------- #
@dataclass
class Pool:
    address: str
    dex: str
    base_mint: str                         # the memecoin
    quote_symbol: str                      # SOL / USDC / ...
    network: str = "solana"
    created_at: Optional[datetime] = None
    liquidity_usd: Optional[float] = None


@dataclass
class Candle:
    """OHLCV bar. ``ts`` is the bar's OPEN time (UTC)."""
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class PriceSeries:
    """Time-ordered candles for one pool, plus the resolution used."""
    mint: str
    pool: Optional[Pool]
    timeframe: str                         # "minute" | "hour" | "day"
    aggregate: int                         # e.g. 1
    candles: list[Candle] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.candles


# --------------------------------------------------------------------------- #
# Safety
# --------------------------------------------------------------------------- #
@dataclass
class SafetyReport:
    mint: str
    # Hard-gate primitives (None = unknown / not fetched):
    mint_authority_revoked: Optional[bool] = None
    freeze_authority_revoked: Optional[bool] = None
    lp_locked_or_burned_pct: Optional[float] = None
    top10_holder_pct: Optional[float] = None
    # Aggregator score (RugCheck score_normalised, 0..100, higher = riskier):
    risk_score: Optional[float] = None
    risks: list[str] = field(default_factory=list)
    source: str = "rugcheck"
    raw: dict[str, Any] = field(default_factory=dict)

    def hard_gate_pass(self, *, require_lp: bool = True, min_lp_pct: float = 90.0) -> bool:
        """True only if the binary, high-confidence blocks all pass. Unknown (None)
        fails closed for authorities; LP check is opt-in (irrelevant pre-graduation)."""
        if self.mint_authority_revoked is not True:
            return False
        if self.freeze_authority_revoked is not True:
            return False
        if require_lp and (self.lp_locked_or_burned_pct or 0.0) < min_lp_pct:
            return False
        return True


# --------------------------------------------------------------------------- #
# Simulation results
# --------------------------------------------------------------------------- #
@dataclass
class FillResult:
    """Outcome of simulating one buy-and-hold-to-horizon trade for one signal."""
    mint: str
    horizon: str
    entry_time: datetime
    entry_price: float
    exit_time: datetime
    exit_price: float
    gross_multiple: float                  # exit/entry before costs
    net_multiple: float                    # after slippage + fees + tip + MEV
    total_cost_fraction: float             # fraction of notional lost to costs (round trip)
    sellable: bool                         # False -> treated as total loss
    notes: list[str] = field(default_factory=list)


@dataclass
class HorizonResult:
    """Aggregate stats for one exit horizon across the whole corpus (full
    denominator: zeros and unsellables included)."""
    horizon: str
    n_trades: int
    n_winners: int                         # net_multiple > 1.0
    n_zeros: int                           # net_multiple ~ 0 / unsellable
    win_rate: float
    mean_net_multiple: float               # arithmetic mean (skew-sensitive, expect tail-driven)
    median_net_multiple: float
    profit_factor: float                   # sum(gains-1) / sum(1-losses) on net multiples
    total_return: float                    # equal-weight portfolio multiple - 1.0
    p25_net_multiple: float
    p75_net_multiple: float
    max_net_multiple: float
    ci95_total_return: tuple[float, float] # bootstrap CI on equal-weight total return


@dataclass
class BacktestReport:
    channel: str
    generated_at: datetime
    n_signals: int
    n_parsed: int                          # had a usable mint
    n_priced: int                          # had locatable price data
    horizons: list[HorizonResult] = field(default_factory=list)
    cost_assumptions: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "generated_at": self.generated_at.isoformat(),
            "n_signals": self.n_signals,
            "n_parsed": self.n_parsed,
            "n_priced": self.n_priced,
            "cost_assumptions": self.cost_assumptions,
            "notes": self.notes,
            "horizons": [asdict(h) for h in self.horizons],
        }
