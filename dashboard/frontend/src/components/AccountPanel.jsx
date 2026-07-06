import React from "react";
import InfoHint from "./InfoHint";

// THE panel that answers "what is my balance" in one glance.
// LIVE paper account only — deliberately separate from the backtest seed stats below it.

const money = (v, dp = 2) => (v < 0 ? "−$" : "$") + Math.abs(v).toFixed(dp);
const signed = (v, dp = 2) =>
  (v > 0 ? "+$" : v < 0 ? "−$" : "$") + Math.abs(v).toFixed(dp);

function Cell({ label, value, sub, tone = "ink" }) {
  const color = {
    win: "text-win",
    loss: "text-loss",
    ink: "text-ink",
    muted: "text-muted",
  }[tone];
  return (
    <div className="min-w-0">
      <div className="text-[12px] tracking-[0.12em] uppercase text-muted/80 font-semibold whitespace-nowrap">
        {label}
      </div>
      <div className={`num text-[16px] font-semibold leading-tight mt-0.5 ${color}`}>{value}</div>
      {sub && <div className="text-[11px] text-muted/70 leading-tight mt-0.5">{sub}</div>}
    </div>
  );
}

export default function AccountPanel({ account, caps, book = "live", wallet = null }) {
  if (!account) return null;
  const a = account;
  const paper = book === "paper";
  const delta = a.balance_usd - a.start_usd;
  const deltaPct = a.start_usd ? (delta / a.start_usd) * 100 : 0;
  const up = delta >= 0;
  const pnlTone = (v) => (v > 0 ? "win" : v < 0 ? "loss" : "muted");
  const since = a.live_since
    ? new Date(a.live_since).toLocaleDateString("en-US", { month: "short", day: "numeric" })
    : "—";

  return (
    <div className="panel panel-hover relative overflow-hidden p-3.5 shrink-0">
      <span
        className={`absolute top-0 left-3 right-3 h-px bg-gradient-to-r from-transparent ${
          up ? "via-win/60" : "via-loss/60"
        } to-transparent`}
      />
      <div className="flex items-center gap-2">
        <span className={`w-1.5 h-1.5 rounded-full pulse-dot ${up ? "bg-win" : "bg-loss"}`} />
        <span className="tile-label">
          {paper ? "account · paper balance (practice + measurement)" : "account · live balance"}
        </span>
        <InfoHint
          text={
            paper
              ? `The paper machine since ${since} — a $500 notional bankroll that takes every call uncapped, and your practice desk (every trade action works here with simulated money). Not your money; also the baseline the live book is judged against.`
              : `Your real-money account since ${since} — anchored to the burner wallet's actual value at go-live; only closed live trades move it. The wallet line below is read straight from chain.`
          }
        />
      </div>
      <div className="flex items-baseline gap-2.5 mt-2 flex-wrap">
        <span
          className={`num text-[40px] font-bold leading-none tracking-tight ${
            up ? "text-win" : "text-loss"
          }`}
          style={{
            textShadow: up
              ? "0 0 22px rgba(61,220,132,0.35)"
              : "0 0 22px rgba(255,81,71,0.30)",
          }}
        >
          {money(a.balance_usd)}
        </span>
        <span className={`num text-[15px] font-semibold ${up ? "text-win" : "text-loss"}`}>
          {signed(delta)} ({(deltaPct >= 0 ? "+" : "−") + Math.abs(deltaPct).toFixed(1)}%)
        </span>
        <span className="num text-[14px] text-muted">vs ${a.start_usd.toFixed(0)} start</span>
      </div>
      <div className="grid grid-cols-4 gap-x-3 gap-y-2.5 mt-3.5">
        <Cell
          label="deployed"
          value={money(a.deployed_usd)}
          sub={caps?.deployedCap != null ? `of $${caps.deployedCap} cap` : undefined}
        />
        <Cell label="dry powder" value={money(a.dry_powder_usd)} />
        <Cell
          label="today closed"
          value={signed(a.today_pnl_usd)}
          sub={`${a.n_closed_today ?? 0} trade${(a.n_closed_today ?? 0) === 1 ? "" : "s"} closed today`}
          tone={pnlTone(a.today_pnl_usd)}
        />
        <Cell
          label="all closed"
          value={signed(a.live_realized_pnl)}
          sub="realized, since live"
          tone={pnlTone(a.live_realized_pnl)}
        />
        <Cell
          label="open p&l"
          value={signed(a.live_unrealized_pnl)}
          sub="open bags, marked now"
          tone={pnlTone(a.live_unrealized_pnl)}
        />
        <Cell
          label="open"
          value={a.n_live_open}
          sub={caps?.maxOpen != null ? `of ${caps.maxOpen} max` : undefined}
        />
        <Cell label="watching" value={a.n_live_watching} />
        <Cell label="closed" value={a.n_live_trades_closed} />
        {!paper && wallet && (
          <Cell
            label="wallet · on-chain"
            value={`${Number(wallet.sol).toFixed(4)} SOL`}
            sub={wallet.usd != null ? `≈ $${Number(wallet.usd).toFixed(2)} (chain truth)` : undefined}
          />
        )}
      </div>
    </div>
  );
}
