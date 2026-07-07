"""Self-awareness — is the system bleeding AS DESIGNED, or genuinely broken?

The expectation is derived from the strategy's OWN backtest distribution (config #1's realized
multiples), not hardcoded. Real values on the fresh corpus are win ~8%, per-trade mean ~1.0 (with
the tail) / ~0.83 without it, drop-top-3 ~0.79, Hill alpha ~1.7 over the entered subset. The
monitor compares live stats to that distribution and classifies:

  - "as designed"     : live win% / ex-tail mean sit inside the expected regime (a normal bleed)
  - "off expectation" : live stats drift outside the bootstrap band (too-good is suspicious too)
  - "feed outage"     : the price/signal feed has gone stale
  - "path deviation"  : a live position diverges from what config #1 would produce on the same candles

It never alarms on a long "days since ≥10x" — that gap is EXPECTED for a power-law tail-rider and is surfaced
as normal. The honest job: say which of {as designed, broken} is true, out loud.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import numpy as np

from memebot.live.state import LiveState, from_iso, utcnow


@dataclass(frozen=True)
class Expectation:
    n: int
    win_rate: float
    mean: float
    ex_tail_mean: float
    ci_lo: float
    ci_hi: float
    total_loss_rate: float
    hill_alpha: float

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


def build_expectation(multiples: list[float], *, seed: int = 0, n_boot: int = 4000) -> Expectation:
    a = np.asarray([m for m in multiples if m is not None], dtype=float)
    if len(a) < 5:
        return Expectation(len(a), 0.0, float(a.mean() if len(a) else 0), 0.0, 0.0, 0.0, 0.0, 0.0)
    rng = np.random.default_rng(seed)
    boot = a[rng.integers(0, len(a), size=(n_boot, len(a)))].mean(axis=1)
    srt = np.sort(a)[::-1]
    ex_tail = float(srt[1:].mean()) if len(srt) > 1 else float(srt.mean())
    pos = np.sort(a[a > 0])[::-1]
    k = max(5, int(len(pos) * 0.10))
    alpha = float(1.0 / np.mean(np.log(pos[:k] / pos[k]))) if len(pos) > k else 0.0
    return Expectation(
        n=len(a), win_rate=float((a > 1).mean()), mean=float(a.mean()), ex_tail_mean=ex_tail,
        ci_lo=float(np.percentile(boot, 2.5)), ci_hi=float(np.percentile(boot, 97.5)),
        total_loss_rate=float((a < 0.1).mean()), hill_alpha=alpha,
    )


@dataclass
class Assessment:
    status: str            # "as_designed" | "off_expectation"
    reasons: list[str]
    live_win_rate: float
    live_mean: float


class Monitor:
    def __init__(self, state: LiveState, expectation: Expectation, *,
                 feed_max_gap_s: float = 1800.0, win_tol: float = 0.10,
                 listener_max_gap_s: float = 600.0, recon_max_gap_s: float = 900.0):
        self.state = state
        self.exp = expectation
        self.feed_max_gap_s = feed_max_gap_s
        self.win_tol = win_tol
        self.listener_max_gap_s = listener_max_gap_s
        self.recon_max_gap_s = recon_max_gap_s

    # -- expectation from the strategy's own realized distribution -------- #
    @classmethod
    def from_closed_trades(cls, state: LiveState, **kw) -> "Monitor":
        # F32: the expectation band must be a FIXED reference, NOT the live population it
        # judges — a sample mean is always ~centred in a bootstrap CI of a superset of
        # itself, so drift could otherwise never fire. Build it from the SEED replay (the
        # backtest distribution, seen_mints.outcome='seen'), frozen at boot; fall back to
        # all closed trades only on an unseeded dev DB.
        seed = cls._provenance_mults(state, live=False)
        if len(seed) < 5:
            seed = [c["realized_multiple"] for c in state.closed_trades()
                    if c["realized_multiple"] is not None]
        return cls(state, build_expectation(seed), **kw)

    @staticmethod
    def _provenance_mults(state: LiveState, *, live: bool) -> list[float]:
        """Realized multiples partitioned by provenance: live=True -> real paper/live trades
        (seen_mints.outcome != 'seen'); live=False -> the seed backtest replay."""
        op = "!=" if live else "="
        rows = state.query(
            "SELECT c.realized_multiple AS m FROM closed_trades c "
            "JOIN seen_mints s ON s.mint = c.mint "
            f"WHERE s.outcome {op} 'seen' AND c.realized_multiple IS NOT NULL")
        return [r["m"] for r in rows]

    # -- drift assessment -------------------------------------------------- #
    def assess(self, live_multiples: list[float]) -> Assessment:
        a = np.asarray([m for m in live_multiples if m is not None], dtype=float)
        reasons: list[str] = []
        if len(a) < 5:
            return Assessment("as_designed", ["too few trades to judge"], 0.0, 0.0)
        win = float((a > 1).mean())
        mean = float(a.mean())
        # win-rate drift beyond tolerance either way (too-good is suspicious, not just too-bad)
        if abs(win - self.exp.win_rate) > self.win_tol:
            reasons.append(f"win% {win:.0%} vs expected {self.exp.win_rate:.0%}")
        # mean outside the bootstrap band
        if mean < self.exp.ci_lo or mean > self.exp.ci_hi:
            reasons.append(f"mean {mean:.3f} outside expected [{self.exp.ci_lo:.3f}, {self.exp.ci_hi:.3f}]")
        status = "off_expectation" if reasons else "as_designed"
        return Assessment(status, reasons or ["bleeding as designed"], win, mean)

    # -- feed health ------------------------------------------------------- #
    def check_feed(self, *, now: Optional[datetime] = None) -> Optional[dict]:
        now = now or utcnow()
        last = from_iso(self.state.get_system("last_feed_ok_ts"))
        if last is None:
            return None
        gap = (now - last).total_seconds()
        if gap > self.feed_max_gap_s:
            return {"severity": "WARN", "kind": "FEED_OUTAGE",
                    "message": f"no feed update in {gap/60:.0f}m (> {self.feed_max_gap_s/60:.0f}m)"}
        return None

    def heartbeat(self, *, now: Optional[datetime] = None) -> None:
        self.state.set_system("last_feed_ok_ts", (now or utcnow()).isoformat())

    # -- listener health (audit #9) --------------------------------------- #
    def check_listener(self, *, now: Optional[datetime] = None) -> Optional[dict]:
        """A deaf-but-connected telethon session stops ingesting calls with a healthy-looking feed.
        The listener writes a 45s heartbeat while connected; if that stamp goes stale beyond the
        threshold the listener coroutine is wedged. (Do NOT threshold on the last-EVENT time — the
        channel can legitimately be quiet for days.) None if never connected (or listener disabled)."""
        last = from_iso(self.state.get_system("last_listener_ok_ts"))
        if last is None:
            return None
        gap = ((now or utcnow()) - last).total_seconds()
        if gap > self.listener_max_gap_s:
            return {"severity": "WARN", "kind": "LISTENER_STALE",
                    "message": f"listener heartbeat stale {gap/60:.0f}m "
                               f"(> {self.listener_max_gap_s/60:.0f}m) — telethon may be wedged/deaf; "
                               "check @your_channel ingestion"}
        return None

    # -- on-chain reconciler health (H7, audit 2026-07-07) ------------------ #
    def check_recon(self, *, now: Optional[datetime] = None) -> Optional[dict]:
        """The on-chain safety net (_reconcile_onchain) refreshes wallet_at every pass. If that
        stamp goes stale the net is dead — a broken RPC would otherwise silently no-op it forever
        while real bags trade uncovered. Checked HERE (the sampler loop) so a different task
        raises the alarm than the one that died. None if wallet_at was never set (paper/dry)."""
        last = from_iso(self.state.get_system("wallet_at"))
        if last is None:
            return None
        gap = ((now or utcnow()) - last).total_seconds()
        if gap > self.recon_max_gap_s:
            return {"severity": "CRIT", "kind": "RECON_STALE",
                    "message": f"on-chain reconciler stale {gap/60:.0f}m "
                               f"(> {self.recon_max_gap_s/60:.0f}m) — the wallet/book safety net "
                               "is not running; check RPC health"}
        return None

    # -- path deviation ---------------------------------------------------- #
    @staticmethod
    def path_deviation(live_multiple: float, sim_multiple: float, *, tol: float = 1e-3) -> Optional[dict]:
        """A live realized multiple should equal what config #1's sim produces on the same candles."""
        if sim_multiple is None or live_multiple is None:
            return None
        denom = max(1.0, abs(sim_multiple))
        if abs(live_multiple - sim_multiple) / denom > tol:
            return {"severity": "CRIT", "kind": "PATH_DEVIATION",
                    "message": f"live {live_multiple:.4f} vs modeled {sim_multiple:.4f}"}
        return None

    # non-manual algo closes only — a human override legitimately diverges from the sim
    _ALGO_CLOSE_SQL = ("c.close_reason NOT IN ('manual_close','manual_stop','dead_writeoff',"
                       "'manual_close_reconciled','crash_gap_reconciled')")

    def check_path(self, *, tol: float = 0.25) -> list[dict]:
        """F03 (wired 2026-07-07): every NEW closed LIVE algo trade is compared against the
        shadow C1 rider's sim result on the SAME ticks. Real fills differ from the sim's fill
        model by slippage, so the tolerance is loose (25%) — this catches gross path divergence
        (a missed stop, a wrong rung count, a booking bug), not fill noise. Incremental via a
        persisted closed_trades high-water."""
        last = int(self.state.get_system("path_check_close_id") or 0)
        rows = self.state.query(
            "SELECT c.id, c.mint, c.realized_multiple AS live_m, s.realized_multiple AS sim_m "
            "FROM closed_trades c "
            "JOIN shadow_trades s ON s.mint = c.mint AND s.config_id = 'C1' "
            "JOIN seen_mints sm ON sm.mint = c.mint AND sm.outcome != 'seen' "
            f"WHERE c.id > ? AND c.realized_multiple IS NOT NULL "
            f"AND s.realized_multiple IS NOT NULL AND {self._ALGO_CLOSE_SQL} "
            "ORDER BY c.id LIMIT 50", (last,))
        out: list[dict] = []
        hi = last
        for r in rows:
            hi = max(hi, r["id"])
            dev = self.path_deviation(r["live_m"], r["sim_m"], tol=tol)
            if dev:
                dev = dict(dev, severity="WARN",
                           message=f"{r['mint'][:6]}… closed live at {r['live_m']:.3f}x but the "
                                   f"C1 sim twin says {r['sim_m']:.3f}x on the same ticks "
                                   "(>25% apart) — check fills/booking for this position")
                out.append(dev)
        if hi != last:
            self.state.set_system("path_check_close_id", str(hi))
        return out

    # -- run one monitoring pass, write alerts ----------------------------- #
    def run_once(self, *, now: Optional[datetime] = None) -> Assessment:
        now = now or utcnow()
        live = self._provenance_mults(self.state, live=True)   # judge LIVE trades only (F32)
        assessment = self.assess(live)
        prev = self.state.get_system("live_status")
        self.state.set_system("live_status", assessment.status)
        # transition-gated like the listener/recon checks (re-audit: an ungated write every 30s
        # floods the alerts table for the length of the outage; notify's throttle re-pages anyway)
        feed = self.check_feed(now=now)
        prev_f = self.state.get_system("feed_ok")
        self.state.set_system("feed_ok", "stale" if feed else "ok")
        if feed and prev_f != "stale":
            self.state.record_alert(**feed, ts=now)
        # listener staleness (audit #9): alert only on the transition into 'stale' (no 30s storm)
        listener = self.check_listener(now=now)
        prev_l = self.state.get_system("listener_ok")
        self.state.set_system("listener_ok", "stale" if listener else "ok")
        if listener and prev_l != "stale":
            self.state.record_alert(**listener, ts=now)
        # dead on-chain reconciler (H7): transition-gated like the listener check
        recon = self.check_recon(now=now)
        prev_r = self.state.get_system("recon_ok")
        self.state.set_system("recon_ok", "stale" if recon else "ok")
        if recon and prev_r != "stale":
            self.state.record_alert(**recon, ts=now)
        # per-trade path deviation vs the C1 sim twin (F03) — each trade checked once
        try:
            for dev in self.check_path():
                self.state.record_alert(**dev, ts=now)
        except Exception:
            pass    # shadow tables may not exist on a bare dev DB — never break the pass
        # Alert only on a TRANSITION into off_expectation (no 30s re-alert storm), and only
        # when the band is real — a degenerate zero-width band built from <5 seed rows must
        # never fire on a merely-positive live mean.
        if (assessment.status == "off_expectation" and prev != "off_expectation"
                and self.exp.n >= 5):
            self.state.record_alert(severity="WARN", kind="DRIFT",
                                    message="; ".join(assessment.reasons), ts=now)
        return assessment
