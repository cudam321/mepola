"""Configuration loading: reads ``config.toml`` (tunables) and ``.env`` (secrets)."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader (no dependency). Existing env vars win."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        os.environ.setdefault(key, val)


@dataclass(frozen=True)
class CostModel:
    latency_seconds: float = 3.0
    entry_slippage_bps: float = 300.0
    exit_slippage_bps: float = 300.0
    pumpfun_fee_bps: float = 95.0
    pumpswap_fee_bps: float = 30.0
    priority_fee_sol: float = 0.001
    jito_tip_sol: float = 0.005
    mev_drag_bps: float = 70.0
    trade_size_sol: float = 0.1

    def fixed_sol_cost_per_tx(self) -> float:
        return self.priority_fee_sol + self.jito_tip_sol


@dataclass(frozen=True)
class Settings:
    chain: str = "solana"
    gecko_network: str = "solana"
    horizons: tuple[str, ...] = ("30s", "5m", "15m", "1h", "4h", "1d")
    unsellable_is_total_loss: bool = True
    cost: CostModel = field(default_factory=CostModel)
    raw: dict[str, Any] = field(default_factory=dict)

    # secrets (from env)
    birdeye_api_key: str = ""
    helius_api_key: str = ""
    jupiter_api_key: str = ""

    @staticmethod
    def load(config_path: Path | None = None, env_path: Path | None = None) -> "Settings":
        config_path = config_path or (PROJECT_ROOT / "config.toml")
        env_path = env_path or (PROJECT_ROOT / ".env")
        _load_dotenv(env_path)

        raw: dict[str, Any] = {}
        if config_path.exists():
            raw = tomllib.loads(config_path.read_text())

        net = raw.get("network", {})
        bt = raw.get("backtest", {})
        cm = raw.get("cost_model", {})

        cost = CostModel(
            latency_seconds=float(cm.get("latency_seconds", 3.0)),
            entry_slippage_bps=float(cm.get("entry_slippage_bps", 300.0)),
            exit_slippage_bps=float(cm.get("exit_slippage_bps", 300.0)),
            pumpfun_fee_bps=float(cm.get("pumpfun_fee_bps", 95.0)),
            pumpswap_fee_bps=float(cm.get("pumpswap_fee_bps", 30.0)),
            priority_fee_sol=float(cm.get("priority_fee_sol", 0.001)),
            jito_tip_sol=float(cm.get("jito_tip_sol", 0.005)),
            mev_drag_bps=float(cm.get("mev_drag_bps", 70.0)),
            trade_size_sol=float(cm.get("trade_size_sol", 0.1)),
        )
        return Settings(
            chain=net.get("chain", "solana"),
            gecko_network=net.get("gecko_network", "solana"),
            horizons=tuple(bt.get("horizons", ("30s", "5m", "15m", "1h", "4h", "1d"))),
            unsellable_is_total_loss=bool(bt.get("unsellable_is_total_loss", True)),
            cost=cost,
            raw=raw,
            birdeye_api_key=os.environ.get("BIRDEYE_API_KEY", ""),
            helius_api_key=os.environ.get("HELIUS_API_KEY", ""),
            jupiter_api_key=os.environ.get("JUPITER_API_KEY", ""),
        )


HORIZON_SECONDS: dict[str, int] = {
    "30s": 30,
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "2h": 7200,
    "4h": 14400,
    "12h": 43200,
    "1d": 86400,
}


def horizon_to_seconds(label: str) -> int:
    if label not in HORIZON_SECONDS:
        raise ValueError(f"unknown horizon {label!r}; known: {sorted(HORIZON_SECONDS)}")
    return HORIZON_SECONDS[label]
