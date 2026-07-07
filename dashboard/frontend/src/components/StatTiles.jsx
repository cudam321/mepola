import React, { useEffect, useState } from "react";
import ScopeToggle from "./ScopeToggle";
import { igniteClass } from "../ignite";
import InfoHint from "./InfoHint";

const SCOPE_NOTE = {
  live: "forward trades in this book",
  seed: "backtest seed (pre-live corpus)",
  all: "forward + seed combined",
};

function Tile({ label, value, sub, tone = "ink", accent = false, glyph, ignite = false }) {
  const color = { win: "text-win", loss: "text-loss", tail: "text-tail", ink: "text-ink" }[tone];
  return (
    <div className="panel panel-hover relative overflow-hidden p-3">
      {accent && (
        <span className="absolute top-0 left-3 right-3 h-px bg-gradient-to-r from-transparent via-tail/70 to-transparent" />
      )}
      <div className="tile-label">{label}</div>
      <div
        className={`num text-[26px] font-semibold leading-tight mt-1 ${color} ${
          ignite ? "ignite" : ""
        }`}
        style={accent && !ignite ? { textShadow: "0 0 18px rgba(203,241,78,0.35)" } : undefined}
      >
        {glyph && <span className="text-[16px] mr-1 align-[2px]">{glyph}</span>}
        {value}
      </div>
      {sub && <div className="text-[14px] text-muted mt-0.5">{sub}</div>}
    </div>
  );
}

export default function StatTiles({ stats }) {
  const [scope, setScope] = useState(null);
  useEffect(() => {
    // default: LIVE if any live trades exist, else SEED
    if (scope === null && stats?.live) setScope(stats.live.n > 0 ? "live" : "seed");
  }, [stats, scope]);

  if (!stats || !stats.seed) return null;
  const sc = scope || (stats.live?.n > 0 ? "live" : "seed");
  const s = stats[sc] || {};
  const usd = (v) => (v >= 0 ? "+$" : "−$") + Math.abs(v).toFixed(0);
  const d = s.days_since_last_10x;
  const tailAgo =
    d == null
      ? "no ≥10x yet"
      : d < 1
      ? `≥10x hit ${Math.max(1, Math.round(d * 24))}h ago`
      : `last ≥10x ${d}d ago`;

  return (
    <div className="flex flex-col gap-2 shrink-0">
      <div className="flex items-center gap-2 px-1">
        <span className="tile-label whitespace-nowrap shrink-0">
          distribution · <span className="text-tail">{sc}</span>
        </span>
        <InfoHint text={SCOPE_NOTE[sc]} />
        <span className="ml-auto">
          <ScopeToggle value={sc} onChange={setScope} />
        </span>
      </div>
      {!s.n ? (
        <div className="panel p-3 flex items-center gap-2 text-[14px] text-muted leading-relaxed">
          <span className="w-1.5 h-1.5 rounded-full bg-live/70 pulse-dot shrink-0" />
          {s.n_open
            ? `${s.n_open} live position${s.n_open > 1 ? "s" : ""} open — no closed outcomes to score yet; the seed distribution is under SEED.`
            : "no live trades yet — the engine is watching; the seed distribution is under SEED."}
        </div>
      ) : (
        <div className="grid grid-cols-2 gap-3">
          <Tile
            label="best multiple"
            value={`${s.best}x`}
            sub={`the tail · ${tailAgo}`}
            tone="tail"
            accent
            ignite={!!igniteClass(s.best)}
          />
          <Tile
            label="win rate"
            value={`${(s.win_rate * 100).toFixed(1)}%`}
            sub={
              s.n_banked
                ? `${s.n - s.n_banked} closed + ${s.n_banked} banked ≥1x`
                : `${s.n} closed trades`
            }
          />
          <Tile
            label="top-1 concentration"
            value={`${s.top1_pnl_pct}%`}
            sub={`top-3 ${s.top3_pnl_pct}% of gains`}
            tone="tail"
          />
          <Tile
            label="per-trade mean"
            value={`${Number(s.mean).toFixed(2)}x`}
            sub={`${Number(s.mean_ex_tail).toFixed(2)}x ex-tail`}
            tone={s.mean_ex_tail < 1 ? "loss" : "win"}
          />
          <Tile
            label="bleed rate"
            value={`${(s.bleed_rate * 100).toFixed(0)}%`}
            sub="of trades end < 1x (as designed)"
            tone="loss"
          />
          <Tile
            label="total-loss rate"
            value={`${(s.total_loss_rate * 100).toFixed(0)}%`}
            sub="of trades end ≈ 0x (rug/dust)"
            tone="loss"
          />
          {/* audit #21: NOT bright-green "win" — this net is entirely one token (ANSEM); ex-tail it is
              a loss. Neutral tone + an on-tile caveat so it never reads as reliable income. Hidden
              entirely when this book carries no seed replay (the live book) — a sim number would be
              paper fiction there (user report 2026-07-06). */}
          {(stats.seed?.n || 0) > 0 && (
            <>
              <Tile
                label="$3-fixed net · sim"
                value={usd(stats.net_fixed_usd)}
                sub={`full history → $${stats.final_fixed_usd} · ~all from 1 token (ANSEM); ex-tail = loss`}
                tone="ink"
                glyph={stats.net_fixed_usd >= 0 ? "▲" : "▼"}
              />
              <Tile
                label="0.6%-frac net · sim"
                value={usd(stats.net_fractional_usd)}
                sub={`full history → $${stats.final_fractional_usd} · ~all from 1 token (ANSEM); ex-tail = loss`}
                tone="ink"
                glyph={stats.net_fractional_usd >= 0 ? "▲" : "▼"}
              />
            </>
          )}
        </div>
      )}
    </div>
  );
}
