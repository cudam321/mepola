"""SQLite state store — the engine's durable memory and the dashboard's read contract.

One file (`runs/live_state.db`, WAL mode). This module is the ONLY writer; the dashboard
opens the same file read-only. All timestamps are ISO-8601 UTC text; multiples are linear
(1.0 = break-even, 0 = total loss).

Design decision (paper == backtest): the `TailRider` state machine embodies config #1's fill
model (the sim's entry x1.01 / TP x0.985 / stop x0.95), so a paper trade's realized multiple
equals the backtest by construction — the equivalence gate literally validates the paper engine.
Positions therefore persist the machine's snapshot fields (entry, rem, pr, n_tp, lvl, secured,
peak_price) so the engine can rebuild in-flight `TailRider`s verbatim after a restart.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

SCHEMA_VERSION = 1

DDL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS seen_mints (
  mint            TEXT PRIMARY KEY,
  ticker          TEXT,
  source_channel  TEXT,
  message_id      INTEGER,
  first_seen_at   TEXT NOT NULL,
  signal_price    REAL,
  outcome         TEXT DEFAULT 'seen'          -- seen | positioned | rejected
);

CREATE TABLE IF NOT EXISTS signals (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  ts              TEXT NOT NULL,
  source_channel  TEXT, message_id INTEGER,
  ticker          TEXT, mint TEXT, side TEXT,
  parse_confidence REAL,
  is_first_call   INTEGER DEFAULT 0,
  accepted        INTEGER DEFAULT 0,
  reject_reason   TEXT,
  raw_text        TEXT
);
CREATE INDEX IF NOT EXISTS idx_signals_ts ON signals(ts);

CREATE TABLE IF NOT EXISTS positions (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  mint            TEXT UNIQUE NOT NULL,
  ticker          TEXT,
  source_channel  TEXT, message_id INTEGER,
  signal_at       TEXT NOT NULL,
  signal_price    REAL NOT NULL,
  state           TEXT NOT NULL,               -- WATCHING|ENTERED|SECURED|RIDING|EXITED|STOPPED|EXPIRED
  dip_deadline    TEXT,
  entry_at        TEXT,
  entry_price     REAL,
  stake_usd       REAL,
  tokens_qty      REAL,
  stop_price      REAL,
  secured         INTEGER DEFAULT 0,
  n_tp            INTEGER DEFAULT 0,
  next_rung_mult  REAL,
  next_rung_price REAL,
  remaining_frac  REAL DEFAULT 1.0,
  proceeds_units  REAL DEFAULT 0.0,            -- the machine's `pr` (price-units)
  peak_price      REAL,
  low_price       REAL,                        -- lowest low seen (dip watermark; display-only)
  t0_epoch        REAL,                        -- dip-window anchor (first-candle epoch)
  current_price   REAL,
  current_multiple REAL,                       -- (pr + rem*current_price)/entry  (mark-to-market)
  realized_multiple REAL,                      -- pr/entry once terminal
  realized_pnl_usd REAL DEFAULT 0.0,
  unrealized_pnl_usd REAL DEFAULT 0.0,
  opened_at       TEXT, updated_at TEXT, closed_at TEXT,
  close_reason    TEXT,
  controller      TEXT DEFAULT 'algo'          -- live owner: 'algo' | 'manual' (also ALTER-added for old DBs)
);
CREATE INDEX IF NOT EXISTS idx_positions_state ON positions(state);

CREATE TABLE IF NOT EXISTS position_events (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  position_id     INTEGER NOT NULL REFERENCES positions(id),
  mint            TEXT NOT NULL,
  ts              TEXT NOT NULL,
  event_type      TEXT NOT NULL,               -- SIGNAL|ENTER|STOP_OUT|TP|RIDE_SELL|MARK|EXPIRE|FINALIZE|CLOSE
  price           REAL,
  rung_mult       REAL,
  frac            REAL,
  proceeds_usd    REAL,
  remaining_frac  REAL,
  note            TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_pos ON position_events(position_id);
CREATE INDEX IF NOT EXISTS idx_events_mint_ts ON position_events(mint, ts);

CREATE TABLE IF NOT EXISTS closed_trades (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  position_id     INTEGER NOT NULL REFERENCES positions(id),
  mint            TEXT NOT NULL, ticker TEXT,
  entry_at        TEXT, entry_price REAL, stake_usd REAL,
  exit_at         TEXT, close_reason TEXT,
  realized_multiple REAL NOT NULL,
  pnl_usd         REAL,
  peak_multiple   REAL, held_hours REAL,
  n_tp INTEGER, was_stopped INTEGER, was_secured INTEGER
);
CREATE INDEX IF NOT EXISTS idx_closed_exit ON closed_trades(exit_at);
CREATE INDEX IF NOT EXISTS idx_closed_mult ON closed_trades(realized_multiple);

CREATE TABLE IF NOT EXISTS bankroll_history (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  ts              TEXT NOT NULL,
  realized_equity_usd   REAL,
  unrealized_equity_usd REAL,
  deployed_usd    REAL, dry_powder_usd REAL,
  n_open INTEGER, n_watching INTEGER,
  realized_pnl_cum_usd REAL,
  expected_equity_usd REAL, expected_lo_usd REAL, expected_hi_usd REAL
);
CREATE INDEX IF NOT EXISTS idx_bankroll_ts ON bankroll_history(ts);

CREATE TABLE IF NOT EXISTS alerts (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  ts              TEXT NOT NULL,
  severity        TEXT NOT NULL,               -- INFO|WARN|CRIT
  kind            TEXT NOT NULL,
  message         TEXT, context_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_alerts_ts ON alerts(ts);

CREATE TABLE IF NOT EXISTS system_state (
  key TEXT PRIMARY KEY, value TEXT, updated_at TEXT
);

-- ============ ADAPTIVE layer (additive; deployed DBs migrate implicitly on boot) ============
-- shadow_riders: in-flight champion/challenger forward-race machines (one per config x mint).
-- Rows exist only while a rider is live; terminal riders are deleted after their trades flush.
CREATE TABLE IF NOT EXISTS shadow_riders (
  config_id       TEXT NOT NULL,
  mint            TEXT NOT NULL,
  snapshot_json   TEXT,
  state           TEXT,
  updated_at      TEXT,
  PRIMARY KEY (config_id, mint)
);

-- shadow_trades: ONE ROW PER LEG. A re-entry config (reentry != None) can produce several
-- legs per token; each leg is an independent 1.0-notional entry whose realized_multiple is
-- that leg's pr/entry — exactly the unit stage37 extends into its train/OOS lists. Aggregating
-- a config therefore means treating every row as one trade (do NOT sum legs into one row).
CREATE TABLE IF NOT EXISTS shadow_trades (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  config_id       TEXT NOT NULL,
  mint            TEXT NOT NULL,
  ticker          TEXT,
  entered_at      TEXT,
  closed_at       TEXT,
  realized_multiple REAL,
  close_reason    TEXT
);
CREATE INDEX IF NOT EXISTS idx_shadow_trades_cfg ON shadow_trades(config_id);

-- research_runs: automated re-measurement verdicts (see live/research.py). Recommendations
-- in verdict_json are ADVISORY ONLY — promotion needs the dashboard human to flip
-- system_state.champion_config_id; the system never self-modifies.
CREATE TABLE IF NOT EXISTS research_runs (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  ts              TEXT,
  status          TEXT,
  verdict_json    TEXT
);

-- ============ MANUAL layer (additive; deployed DBs migrate implicitly on boot) ============
-- Human discretionary control ALONGSIDE the autonomous config-#1 machine. A position is owned
-- by exactly one controller (positions.controller: 'algo' | 'manual'); "take over" transfers it.
-- Every manual order that moves real money rides the SAME safe executor + off-loop pipeline as the
-- algo (confirm-then-commit, idempotent sells, breaker, burner allowlist, arming gates). See
-- docs/MANUAL_TRADING_PLAN.md.
--
-- orders: the resting/conditional order book. One row per human intent (market/limit/TP/SL/trailing).
-- position_id is NULL for a not-yet-filled ENTRY order (it attaches on fill). A filled order goes
-- to status='filled'; a multi-rung TP ladder = several rows. status='submitted' is the durable
-- in-flight marker resolved on boot by the same on-chain reconcile that adopts algo buys.
CREATE TABLE IF NOT EXISTS orders (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  mint            TEXT NOT NULL,
  ticker          TEXT,
  position_id     INTEGER,                     -- NULL until an entry order fills / attaches
  kind            TEXT NOT NULL,               -- market | limit | take_profit | stop_loss | trailing_stop
  side            TEXT NOT NULL,               -- buy | sell
  trigger_type    TEXT NOT NULL,               -- now | price_at_or_below | price_at_or_above | mult_at_or_above | peak_drawdown_pct
  trigger_value   REAL,                        -- price / mult / drawdown-fraction (per trigger_type)
  size_kind       TEXT NOT NULL,               -- usd (buys) | token_frac | token_abs (sells)
  size_value      REAL NOT NULL,
  status          TEXT NOT NULL DEFAULT 'open', -- open | submitted | filled | cancelled | expired | failed
  hwm             REAL,                        -- trailing_stop running high-water mark (price)
  note            TEXT,
  created_by      TEXT DEFAULT 'manual',
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL,
  filled_at       TEXT,
  expires_at      TEXT                         -- NULL = GTC
);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_mint ON orders(mint);

-- watchlist: tokens the human is tracking (live price + charts) without a position yet. An entry
-- order on a watched mint creates a manual position when it fills.
CREATE TABLE IF NOT EXISTS watchlist (
  mint            TEXT PRIMARY KEY,
  ticker          TEXT,
  note            TEXT,
  source          TEXT DEFAULT 'manual',
  added_at        TEXT NOT NULL
);

-- manual_signals: the human injecting a token as a CALL (their own intel). The dashboard (a separate
-- process) inserts a 'pending' row; the engine loop consumes it → ingest_call → a WATCHING algo
-- position that config #1 rides, exactly like a channel signal.
CREATE TABLE IF NOT EXISTS manual_signals (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  mint            TEXT NOT NULL,
  ticker          TEXT,
  price           REAL,
  status          TEXT NOT NULL DEFAULT 'pending',   -- pending | done | rejected
  note            TEXT,
  created_at      TEXT NOT NULL,
  processed_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_manual_signals_status ON manual_signals(status);

CREATE VIEW IF NOT EXISTS v_multiples AS
  SELECT mint, ticker, realized_multiple AS multiple, 'realized' AS kind, exit_at AS ts
    FROM closed_trades
  UNION ALL
  SELECT mint, ticker, current_multiple AS multiple, 'unrealized' AS kind, updated_at AS ts
    FROM positions
   WHERE state IN ('ENTERED','SECURED','RIDING');
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.astimezone(timezone.utc).isoformat() if dt else None


def from_iso(s: Optional[str]) -> Optional[datetime]:
    return datetime.fromisoformat(s) if s else None


class LiveState:
    """Thin, typed wrapper over the SQLite DB. The single writer."""

    def __init__(self, path: str | Path, *, read_only: bool = False):
        self.path = str(path)
        self.read_only = read_only
        if read_only:
            uri = f"file:{Path(self.path).as_posix()}?mode=ro"
            self.conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA busy_timeout=5000")
        else:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(self.path, check_same_thread=False)
            self.conn.row_factory = sqlite3.Row      # must precede any get_system() read
            # Two writers touch this file (the engine process + the dashboard's control
            # POSTs, plus the weekly research worker on its OWN connection). WAL allows a
            # single writer at a time; busy_timeout makes the others WAIT for the lock
            # instead of raising 'database is locked'. It is per-connection — set on every
            # open, before the first write (the WAL switch in the DDL below).
            self.conn.execute("PRAGMA busy_timeout=5000")
            self.conn.executescript(DDL)
            # additive migration for DBs created before the column existed (e.g. the
            # deployed Railway volume): CREATE ... IF NOT EXISTS won't add new columns
            # audit #28: swallow ONLY "duplicate column" (the expected additive-migration case);
            # re-raise any other OperationalError (locked / disk I/O) so a real half-migration is loud.
            try:
                self.conn.execute("ALTER TABLE positions ADD COLUMN low_price REAL")
                self.conn.commit()
            except sqlite3.OperationalError as e:
                if "duplicate column" not in str(e).lower():
                    raise
            # MANUAL layer: the live owner of a position ('algo' | 'manual'). Existing rows
            # (the algo's) default to 'algo'; a manual buy / take-over sets 'manual'.
            try:
                self.conn.execute(
                    "ALTER TABLE positions ADD COLUMN controller TEXT DEFAULT 'algo'")
                self.conn.commit()
            except sqlite3.OperationalError as e:
                if "duplicate column" not in str(e).lower():
                    raise
            # Crash-idempotence for the per-leg shadow flush: record_shadow_trade commits
            # before the rider's flushed-watermark does, so a kill in between would replay
            # legs on reboot. Dedup once, then a UNIQUE leg index + INSERT OR IGNORE makes
            # replays no-ops. Guarded: a failure here must never block the engine boot.
            # Run the (full-table) dedup + unique-index build ONCE — only until the index
            # exists. After that it is a no-op guarded by INSERT OR IGNORE, so re-running
            # the scan on every construction (each dashboard control POST opens a writable
            # LiveState) would needlessly full-scan shadow_trades and contend for the lock.
            try:
                have_idx = self.conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='index' "
                    "AND name='ux_shadow_trades_leg'").fetchone()
                if not have_idx:
                    self.conn.execute(
                        "DELETE FROM shadow_trades WHERE id NOT IN (SELECT MIN(id) FROM "
                        "shadow_trades GROUP BY config_id, mint, entered_at, closed_at)")
                    self.conn.execute(
                        "CREATE UNIQUE INDEX IF NOT EXISTS ux_shadow_trades_leg "
                        "ON shadow_trades(config_id, mint, entered_at, closed_at)")
                    self.conn.commit()
            except sqlite3.OperationalError:
                pass
            self._init_system()

    # -- lifecycle ---------------------------------------------------------- #
    def _init_system(self) -> None:
        self.set_system("schema_version", str(SCHEMA_VERSION), only_if_absent=True)
        self.set_system("mode", "paper", only_if_absent=True)
        self.set_system("kill_switch", "off", only_if_absent=True)
        self.set_system("bankroll_start_usd", "500.0", only_if_absent=True)
        # Adaptive-layer keys (consumed by the dashboard + orchestrator):
        #   champion_config_id : the config the REAL (paper/live) engine trades. Default "C1".
        #                        Changed ONLY by a human via the dashboard, never by code.
        #   research_requested : "1" = the dashboard asked for an on-demand re-measurement;
        #                        the orchestrator clears it back to "0" when it launches the run.
        #   last_research_at   : ISO ts of the last research run (set by research.py; the
        #                        orchestrator auto-runs when absent or older than 7 days).
        self.set_system("champion_config_id", "C1", only_if_absent=True)
        # MANUAL layer caps (editable from the dashboard controls):
        #   manual_cap_usd            : total manual exposure ceiling (manual can't crowd out the algo)
        #   manual_trade_hard_cap_usd : per-order fat-finger clamp on a single manual BUY
        self.set_system("manual_cap_usd", "50.0", only_if_absent=True)
        self.set_system("manual_trade_hard_cap_usd", "10.0", only_if_absent=True)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "LiveState":
        return self

    def __exit__(self, *a) -> None:
        self.close()

    # -- system_state (control plane) -------------------------------------- #
    def set_system(self, key: str, value: str, *, only_if_absent: bool = False) -> None:
        if only_if_absent and self.get_system(key) is not None:
            return
        self.conn.execute(
            "INSERT INTO system_state(key,value,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, value, to_iso(utcnow())),
        )
        self.conn.commit()

    def get_system(self, key: str, default: Optional[str] = None) -> Optional[str]:
        row = self.conn.execute("SELECT value FROM system_state WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    # -- seen mints (first-call dedup) ------------------------------------- #
    def is_seen(self, mint: str) -> bool:
        return self.conn.execute("SELECT 1 FROM seen_mints WHERE mint=?", (mint,)).fetchone() is not None

    def mark_seen(self, mint: str, *, ticker=None, source_channel=None, message_id=None,
                  first_seen_at: Optional[datetime] = None, signal_price=None, outcome="seen") -> None:
        self.conn.execute(
            "INSERT INTO seen_mints(mint,ticker,source_channel,message_id,first_seen_at,signal_price,outcome) "
            "VALUES(?,?,?,?,?,?,?) ON CONFLICT(mint) DO UPDATE SET outcome=excluded.outcome",
            (mint, ticker, source_channel, message_id, to_iso(first_seen_at or utcnow()), signal_price, outcome),
        )
        self.conn.commit()

    # -- signals feed ------------------------------------------------------ #
    def record_signal(self, *, ts: datetime, source_channel, message_id, ticker, mint, side,
                      parse_confidence=0.0, is_first_call=False, accepted=False,
                      reject_reason=None, raw_text=None) -> int:
        cur = self.conn.execute(
            "INSERT INTO signals(ts,source_channel,message_id,ticker,mint,side,parse_confidence,"
            "is_first_call,accepted,reject_reason,raw_text) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (to_iso(ts), source_channel, message_id, ticker, mint, side, parse_confidence,
             int(is_first_call), int(accepted), reject_reason, raw_text),
        )
        self.conn.commit()
        return cur.lastrowid

    # -- positions --------------------------------------------------------- #
    def create_position(self, *, mint, ticker, signal_at: datetime, signal_price, state,
                        dip_deadline: Optional[datetime] = None, source_channel=None,
                        message_id=None, t0_epoch=None) -> int:
        now = to_iso(utcnow())
        cur = self.conn.execute(
            "INSERT INTO positions(mint,ticker,source_channel,message_id,signal_at,signal_price,"
            "state,dip_deadline,t0_epoch,opened_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (mint, ticker, source_channel, message_id, to_iso(signal_at), signal_price,
             state, to_iso(dip_deadline), t0_epoch, now, now),
        )
        self.conn.commit()
        return cur.lastrowid

    def _columns(self, table: str) -> set[str]:
        """Real column names for a table (cached). Used to whitelist update_* keys (audit #28):
        the SET clause interpolates the dict KEYS, so a future route forwarding client-controlled
        keys would otherwise be SQL injection into positions/orders. `table` is a hardcoded literal."""
        cache = self.__dict__.setdefault("_col_cache", {})
        if table not in cache:
            cache[table] = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
        return cache[table]

    def update_position(self, mint: str, **fields) -> None:
        if not fields:
            return
        bad = set(fields) - self._columns("positions")
        if bad:
            raise ValueError(f"update_position: unknown column(s) {sorted(bad)}")
        fields["updated_at"] = to_iso(utcnow())
        cols = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE positions SET {cols} WHERE mint=?", (*fields.values(), mint))
        self.conn.commit()

    def get_position(self, mint: str) -> Optional[dict]:
        row = self.conn.execute("SELECT * FROM positions WHERE mint=?", (mint,)).fetchone()
        return dict(row) if row else None

    def active_positions(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM positions WHERE state IN ('WATCHING','ENTERED','SECURED','RIDING') ORDER BY opened_at"
        ).fetchall()
        return [dict(r) for r in rows]

    def all_positions(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM positions ORDER BY opened_at").fetchall()]

    # -- lifecycle events -------------------------------------------------- #
    def append_event(self, *, position_id, mint, ts: datetime, event_type, price=None,
                     rung_mult=None, frac=None, proceeds_usd=None, remaining_frac=None, note="") -> None:
        self.conn.execute(
            "INSERT INTO position_events(position_id,mint,ts,event_type,price,rung_mult,frac,"
            "proceeds_usd,remaining_frac,note) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (position_id, mint, to_iso(ts), event_type, price, rung_mult, frac, proceeds_usd,
             remaining_frac, note),
        )
        self.conn.commit()

    def events_for(self, mint: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM position_events WHERE mint=? ORDER BY ts", (mint,)).fetchall()
        return [dict(r) for r in rows]

    # -- closed trades ----------------------------------------------------- #
    def record_close(self, *, position_id, mint, ticker, entry_at: Optional[datetime], entry_price,
                     stake_usd, exit_at: datetime, close_reason, realized_multiple, pnl_usd,
                     peak_multiple=None, held_hours=None, n_tp=0, was_stopped=False,
                     was_secured=False) -> None:
        self.conn.execute(
            "INSERT INTO closed_trades(position_id,mint,ticker,entry_at,entry_price,stake_usd,exit_at,"
            "close_reason,realized_multiple,pnl_usd,peak_multiple,held_hours,n_tp,was_stopped,was_secured) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (position_id, mint, ticker, to_iso(entry_at), entry_price, stake_usd, to_iso(exit_at),
             close_reason, realized_multiple, pnl_usd, peak_multiple, held_hours, n_tp,
             int(was_stopped), int(was_secured)),
        )
        self.conn.commit()

    def closed_trades(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM closed_trades ORDER BY exit_at").fetchall()]

    # -- bankroll ---------------------------------------------------------- #
    def sample_bankroll(self, *, ts: Optional[datetime] = None, realized_equity_usd, unrealized_equity_usd,
                        deployed_usd, dry_powder_usd, n_open, n_watching, realized_pnl_cum_usd,
                        expected_equity_usd=None, expected_lo_usd=None, expected_hi_usd=None) -> None:
        self.conn.execute(
            "INSERT INTO bankroll_history(ts,realized_equity_usd,unrealized_equity_usd,deployed_usd,"
            "dry_powder_usd,n_open,n_watching,realized_pnl_cum_usd,expected_equity_usd,expected_lo_usd,"
            "expected_hi_usd) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (to_iso(ts or utcnow()), realized_equity_usd, unrealized_equity_usd, deployed_usd,
             dry_powder_usd, n_open, n_watching, realized_pnl_cum_usd, expected_equity_usd,
             expected_lo_usd, expected_hi_usd),
        )
        self.conn.commit()

    def bankroll_series(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM bankroll_history ORDER BY ts").fetchall()]

    # -- alerts ------------------------------------------------------------ #
    def record_alert(self, *, severity, kind, message, context: Optional[dict] = None,
                     ts: Optional[datetime] = None) -> None:
        self.conn.execute(
            "INSERT INTO alerts(ts,severity,kind,message,context_json) VALUES(?,?,?,?,?)",
            (to_iso(ts or utcnow()), severity, kind, message, json.dumps(context or {})),
        )
        self.conn.commit()

    def recent_alerts(self, limit: int = 50) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM alerts ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    # -- adaptive layer: shadow riders / shadow trades / research runs ----- #
    def upsert_shadow_rider(self, config_id: str, mint: str, snapshot: dict, state: str) -> None:
        self.conn.execute(
            "INSERT INTO shadow_riders(config_id,mint,snapshot_json,state,updated_at) VALUES(?,?,?,?,?) "
            "ON CONFLICT(config_id,mint) DO UPDATE SET snapshot_json=excluded.snapshot_json, "
            "state=excluded.state, updated_at=excluded.updated_at",
            (config_id, mint, json.dumps(snapshot), state, to_iso(utcnow())),
        )
        self.conn.commit()

    def load_shadow_riders(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM shadow_riders").fetchall()]

    def delete_shadow_rider(self, config_id: str, mint: str) -> None:
        self.conn.execute("DELETE FROM shadow_riders WHERE config_id=? AND mint=?", (config_id, mint))
        self.conn.commit()

    def delete_shadow_config(self, config_id: str) -> None:
        """Remove a config's whole race footprint (riders + closed legs). Used when a
        user-defined challenger is deleted — its id must leave no trace to inherit."""
        self.conn.execute("DELETE FROM shadow_riders WHERE config_id = ?", (config_id,))
        self.conn.execute("DELETE FROM shadow_trades WHERE config_id = ?", (config_id,))
        self.conn.commit()

    def record_shadow_trade(self, *, config_id, mint, ticker=None, entered_at=None, closed_at=None,
                            realized_multiple=None, close_reason=None) -> None:
        """One row PER LEG (a re-entry config writes several rows for one token).
        OR IGNORE + the ux_shadow_trades_leg unique index make a replayed flush a no-op."""
        self.conn.execute(
            "INSERT OR IGNORE INTO shadow_trades(config_id,mint,ticker,entered_at,closed_at,"
            "realized_multiple,close_reason) VALUES(?,?,?,?,?,?,?)",
            (config_id, mint, ticker, entered_at, closed_at, realized_multiple, close_reason),
        )
        self.conn.commit()

    def shadow_trades_by_config(self) -> dict[str, list[dict]]:
        out: dict[str, list[dict]] = {}
        for r in self.conn.execute("SELECT * FROM shadow_trades ORDER BY closed_at").fetchall():
            out.setdefault(r["config_id"], []).append(dict(r))
        return out

    def record_research_run(self, *, ts: Optional[datetime] = None, status: str, verdict: dict) -> int:
        cur = self.conn.execute(
            "INSERT INTO research_runs(ts,status,verdict_json) VALUES(?,?,?)",
            (to_iso(ts or utcnow()), status, json.dumps(verdict)),
        )
        self.conn.commit()
        return cur.lastrowid

    def latest_research_run(self) -> Optional[dict]:
        row = self.conn.execute("SELECT * FROM research_runs ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    # -- manual layer: orders --------------------------------------------- #
    def create_order(self, *, mint, kind, side, trigger_type, size_kind, size_value,
                     ticker=None, position_id=None, trigger_value=None, status="open",
                     note=None, created_by="manual", expires_at: Optional[datetime] = None,
                     hwm=None) -> int:
        now = to_iso(utcnow())
        cur = self.conn.execute(
            "INSERT INTO orders(mint,ticker,position_id,kind,side,trigger_type,trigger_value,"
            "size_kind,size_value,status,hwm,note,created_by,created_at,updated_at,expires_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (mint, ticker, position_id, kind, side, trigger_type, trigger_value, size_kind,
             size_value, status, hwm, note, created_by, now, now, to_iso(expires_at)),
        )
        self.conn.commit()
        return cur.lastrowid

    def update_order(self, order_id: int, **fields) -> None:
        if not fields:
            return
        bad = set(fields) - self._columns("orders")
        if bad:
            raise ValueError(f"update_order: unknown column(s) {sorted(bad)}")
        fields["updated_at"] = to_iso(utcnow())
        cols = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE orders SET {cols} WHERE id=?", (*fields.values(), order_id))
        self.conn.commit()

    def get_order(self, order_id: int) -> Optional[dict]:
        row = self.conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        return dict(row) if row else None

    def open_orders(self, mint: Optional[str] = None) -> list[dict]:
        """All actionable (open/submitted) orders, optionally for one mint — the evaluator's input."""
        if mint is None:
            rows = self.conn.execute(
                "SELECT * FROM orders WHERE status IN ('open','submitted') ORDER BY id").fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM orders WHERE mint=? AND status IN ('open','submitted') ORDER BY id",
                (mint,)).fetchall()
        return [dict(r) for r in rows]

    def orders_for(self, mint: str) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM orders WHERE mint=? ORDER BY id DESC", (mint,)).fetchall()]

    def all_orders(self, limit: int = 200) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM orders ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]

    def mints_with_open_orders(self) -> list[str]:
        return [r["mint"] for r in self.conn.execute(
            "SELECT DISTINCT mint FROM orders WHERE status IN ('open','submitted')").fetchall()]

    # -- manual layer: watchlist ------------------------------------------ #
    def add_watch(self, mint: str, *, ticker=None, note=None, source="manual") -> None:
        self.conn.execute(
            "INSERT INTO watchlist(mint,ticker,note,source,added_at) VALUES(?,?,?,?,?) "
            "ON CONFLICT(mint) DO UPDATE SET ticker=COALESCE(excluded.ticker,watchlist.ticker), "
            "note=COALESCE(excluded.note,watchlist.note)",
            (mint, ticker, note, source, to_iso(utcnow())),
        )
        self.conn.commit()

    def remove_watch(self, mint: str) -> None:
        self.conn.execute("DELETE FROM watchlist WHERE mint=?", (mint,))
        self.conn.commit()

    def is_watched(self, mint: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM watchlist WHERE mint=?", (mint,)).fetchone() is not None

    def watchlist(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM watchlist ORDER BY added_at DESC").fetchall()]

    # -- manual layer: injected signals (the human's own "calls") --------- #
    def add_manual_signal(self, mint: str, *, ticker=None, price=None, note=None) -> int:
        cur = self.conn.execute(
            "INSERT INTO manual_signals(mint,ticker,price,status,note,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (mint, ticker, price, "pending", note, to_iso(utcnow())))
        self.conn.commit()
        return cur.lastrowid

    def pending_manual_signals(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM manual_signals WHERE status='pending' ORDER BY id").fetchall()]

    def mark_manual_signal(self, sid: int, status: str, note: Optional[str] = None) -> None:
        self.conn.execute(
            "UPDATE manual_signals SET status=?, note=COALESCE(?,note), processed_at=? WHERE id=?",
            (status, note, to_iso(utcnow()), sid))
        self.conn.commit()

    # -- dashboard read helpers ------------------------------------------- #
    def multiples(self) -> list[dict]:
        """Every position's multiple (realized + live) — the power-law hero's data."""
        rows = self.conn.execute("SELECT mint, ticker, multiple, kind, ts FROM v_multiples").fetchall()
        return [dict(r) for r in rows if r["multiple"] is not None]

    def query(self, sql: str, params: tuple = ()) -> list[dict]:
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]
