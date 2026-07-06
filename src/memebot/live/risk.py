"""Risk governor — the integrity guardrail, in code.

Enforces tail-bet sizing: a small FIXED stake, concurrency / deployed / daily-loss caps, and a
global kill-switch. There is deliberately NO code path that scales the stake up with equity
beyond an optional bounded fraction mode — the strategy is size-fragile and must never be levered.

Paper vs live (important): the backtest takes EVERY first-call, so to keep paper == backtest the
concurrency and deployed caps do NOT bind in paper mode; they are real safety limits in LIVE mode
only. The kill-switch always applies. If a cap ever binds in paper it surfaces as monitor drift.

Runtime overrides: the dashboard control API (POST /api/control) may write sizing/risk knobs into
system_state (ctl_stake_usd, ctl_max_concurrent, ctl_total_deployed_cap_usd, ctl_daily_loss_cap_usd);
the governor reads them live. The STRATEGY parameters (dip / stop / TP ladder / re-entry) are
research-locked elsewhere and have NO override path here or anywhere.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Optional

from memebot.live.state import LiveState, to_iso, utcnow

# Size-fragility hard cap, measured by the research (scripts/stage39): $10-fixed/trade survived
# the forward window (-> $1815) but $25-fixed busted the bankroll to $0. EVERY effective-stake
# path clamps to this; no runtime override may exceed it.
STAKE_HARD_CAP_USD = 10.0


@dataclass(frozen=True)
class RiskConfig:
    bankroll_usd: float = 500.0
    stake_mode: str = "fixed"          # "fixed" | "fraction"
    stake_usd: float = 3.0             # fixed $ per token
    stake_fraction: float = 0.005      # fraction of realized equity (fraction mode)
    max_concurrent: int = 25           # LIVE-only safety cap on concurrent open positions
    total_deployed_cap_usd: float = 200.0   # LIVE-only cap on summed open stakes
    daily_loss_cap_usd: float = 50.0        # trips the kill-switch when breached (both modes)


@dataclass(frozen=True)
class EntryDecision:
    ok: bool
    reason: Optional[str] = None       # None on accept; else max_concurrent|deployed_cap|kill|daily_loss


class RiskGovernor:
    def __init__(self, state: LiveState, cfg: RiskConfig):
        self.state = state
        self.cfg = cfg

    # -- runtime overrides (control plane) ---------------------------------- #
    # ctl_* keys in system_state are set by the dashboard control API; strategy parameters are
    # locked elsewhere. All helpers are None-safe: with state=None they fall back to self.cfg.
    def _sys(self, key: str) -> Optional[str]:
        if self.state is None:
            return None
        return self.state.get_system(key)

    def _ov_float(self, key: str, default: float) -> float:
        raw = self._sys(key)
        if raw is None:
            return default
        try:
            return float(raw)
        except (TypeError, ValueError):
            return default

    def _ov_int(self, key: str, default: int) -> int:
        raw = self._sys(key)
        if raw is None:
            return default
        try:
            return int(float(raw))
        except (TypeError, ValueError):
            return default

    def effective_cfg(self) -> RiskConfig:
        """self.cfg with any runtime overrides applied; stake always clamped to the hard cap."""
        return replace(
            self.cfg,
            stake_usd=min(self._ov_float("ctl_stake_usd", self.cfg.stake_usd), STAKE_HARD_CAP_USD),
            max_concurrent=self._ov_int("ctl_max_concurrent", self.cfg.max_concurrent),
            total_deployed_cap_usd=self._ov_float("ctl_total_deployed_cap_usd",
                                                  self.cfg.total_deployed_cap_usd),
            daily_loss_cap_usd=self._ov_float("ctl_daily_loss_cap_usd", self.cfg.daily_loss_cap_usd),
        )

    # -- sizing ------------------------------------------------------------ #
    def size_for(self, realized_equity_usd: Optional[float] = None) -> float:
        if self.cfg.stake_mode == "fraction":
            equity = realized_equity_usd if realized_equity_usd is not None else self.cfg.bankroll_usd
            return min(round(self.cfg.stake_fraction * max(equity, 0.0), 6), STAKE_HARD_CAP_USD)
        return min(self._ov_float("ctl_stake_usd", self.cfg.stake_usd), STAKE_HARD_CAP_USD)

    # -- kill switch ------------------------------------------------------- #
    @property
    def kill_on(self) -> bool:
        return self.state.get_system("kill_switch", "off") == "on"

    def trip_kill(self, reason: str = "") -> None:
        self.state.set_system("kill_switch", "on")
        self.state.record_alert(severity="CRIT", kind="KILL_SWITCH",
                                message=f"kill-switch tripped: {reason}", context={"reason": reason})

    def reset_kill(self) -> None:
        self.state.set_system("kill_switch", "off")

    # -- entry gate -------------------------------------------------------- #
    @staticmethod
    def evaluate(*, mode: str, kill: bool, n_active: int, deployed_usd: float,
                 prospective_stake: float, daily_loss_usd: float, cfg: RiskConfig,
                 reserved_n: int = 0, reserved_usd: float = 0.0) -> EntryDecision:
        """Pure decision. Only the kill-switch binds in paper (mirror the backtest: take EVERY call);
        the daily-loss / concurrency / deployed caps are LIVE-only safety limits (audit #14 — a paper
        daily-loss cap would permanently halt the measurement). `reserved_n`/`reserved_usd` are IN-FLIGHT
        (submitted-but-unconfirmed) buys — counting them is what holds the cap under cross-mint
        concurrency: a correlated -50% dip fires N ENTERs in one poll sweep before ANY confirms, and a
        confirmed-only count would let them all pass and overshoot the ceiling (audit re-verify #1/#2)."""
        if kill:
            return EntryDecision(False, "kill")
        if mode == "paper":
            return EntryDecision(True, None)
        # live safety caps (in-flight buys count too — else a one-sweep correlated dip overshoots)
        if daily_loss_usd >= cfg.daily_loss_cap_usd:
            return EntryDecision(False, "daily_loss")
        if n_active + reserved_n >= cfg.max_concurrent:
            return EntryDecision(False, "max_concurrent")
        if deployed_usd + reserved_usd + prospective_stake > cfg.total_deployed_cap_usd:
            return EntryDecision(False, "deployed_cap")
        return EntryDecision(True, None)

    def can_enter(self, *, prospective_stake: Optional[float] = None,
                  reserved_n: int = 0, reserved_usd: float = 0.0) -> EntryDecision:
        mode = self.state.get_system("mode", "paper")
        stake = prospective_stake if prospective_stake is not None else self.size_for()
        n_active = len(self.state.query(
            "SELECT id FROM positions WHERE state IN ('ENTERED','SECURED','RIDING')"))
        deployed = self.state.query(
            "SELECT COALESCE(SUM(stake_usd),0) AS d FROM positions "
            "WHERE state IN ('ENTERED','SECURED','RIDING')")[0]["d"] or 0.0
        return self.evaluate(mode=mode, kill=self.kill_on, n_active=n_active, deployed_usd=deployed,
                             prospective_stake=stake, daily_loss_usd=self._daily_loss(),
                             cfg=self.effective_cfg(), reserved_n=reserved_n, reserved_usd=reserved_usd)

    # -- daily loss accounting -------------------------------------------- #
    def _today(self) -> str:
        return utcnow().astimezone(timezone.utc).strftime("%Y-%m-%d")

    def _daily_loss(self) -> float:
        if self.state.get_system("daily_loss_date") != self._today():
            return 0.0
        return float(self.state.get_system("daily_loss_usd", "0") or 0.0)

    def record_realized_pnl(self, pnl_usd: float) -> None:
        """Accrue realized losses toward the daily cap; trip the kill-switch on breach."""
        today = self._today()
        if self.state.get_system("daily_loss_date") != today:
            self.state.set_system("daily_loss_date", today)
            self.state.set_system("daily_loss_usd", "0")
        loss = self._daily_loss()
        if pnl_usd < 0:
            loss += -pnl_usd
            self.state.set_system("daily_loss_usd", f"{loss:.6f}")
        cap = self.effective_cfg().daily_loss_cap_usd
        # audit #14: the daily-loss kill is a LIVE limit — never let a paper bleed permanently halt the
        # 24/7 take-every-call measurement (the kill is sticky and has no auto-reset). Accrue in both
        # modes for observability; trip only in live.
        if (loss >= cap and not self.kill_on
                and self.state.get_system("mode", "paper") == "live"):
            self.trip_kill(reason=f"daily loss ${loss:.2f} >= cap ${cap:.2f}")
