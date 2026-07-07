import React, { useEffect, useState } from "react";
import { fetchSnapshot, connectWS, currentBook, setCurrentBook, withBook } from "./api";
import PowerLawHero from "./components/PowerLawHero";
import AccountPanel from "./components/AccountPanel";
import StatTiles from "./components/StatTiles";
import ScopeToggle from "./components/ScopeToggle";
import EquityCurve from "./components/EquityCurve";
import DailyPnl from "./components/DailyPnl";
import StrategyLab from "./components/StrategyLab";
import PositionsTable from "./components/PositionsTable";
import TradeHistory from "./components/TradeHistory";
import AlertsPanel from "./components/AlertsPanel";
import HillPanel from "./components/HillPanel";
import TokenModal from "./components/TokenModal";
import ControlsModal from "./components/ControlsModal";
import InfoHint from "./components/InfoHint";

// The design's logo (verbatim from the Phosphor CRT reference): the power law as a
// mark — a trail of small dots rising into the one big haloed winner.
function LogoGlyph() {
  return (
    <svg width="30" height="30" viewBox="0 0 64 64" fill="none" className="shrink-0 mt-0.5" aria-hidden="true">
      <circle cx="12" cy="52" r="3" fill="#CBF14E" opacity="0.35" />
      <circle cx="24" cy="41" r="4" fill="#CBF14E" opacity="0.55" />
      <circle cx="36" cy="29" r="5" fill="#CBF14E" opacity="0.78" />
      <circle cx="49" cy="15" r="13" fill="#CBF14E" opacity="0.2" />
      <circle cx="49" cy="15" r="8" fill="#CBF14E" />
    </svg>
  );
}

// The logo IS text — the 5-row block-ASCII MEPOLA banner, bright phosphor.
function LogoMark() {
  return (
    <pre className="mepola-wm-ascii shrink-0" aria-label="MEPOLA">
      {`█   █ ████ ███  ████ █    ████
██ ██ █    █  █ █  █ █    █  █
█ █ █ ███  ███  █  █ █    ████
█   █ █    █    █  █ █    █  █
█   █ ████ █    ████ ████ █  █`}
    </pre>
  );
}

function LegendPill({ c, t }) {
  return (
    <span className="flex items-center gap-1.5 text-[13px] text-muted px-2 py-0.5 border border-edge bg-black/30">
      <span className="w-1.5 h-1.5 shrink-0" style={{ background: c }} />
      {t}
    </span>
  );
}

export default function App() {
  const [snap, setSnap] = useState(null);
  const [feed, setFeed] = useState("…");
  const [sel, setSel] = useState(null);
  const [updated, setUpdated] = useState(null);
  const [showControls, setShowControls] = useState(false);
  const [heroScope, setHeroScope] = useState("all");
  const [eqTab, setEqTab] = useState("live");
  const [caps, setCaps] = useState(null);
  // BOOK: which machine you're viewing — LIVE (real money) or PAPER (the measurement twin that
  // keeps paper-trading every call). Paper view is read-only; toggling remounts the data views.
  const [book, setBookState] = useState(currentBook());
  const readOnly = book === "paper";
  const switchBook = (b) => {
    setCurrentBook(b);
    setSnap(null);
    setBookState(b);
  };

  useEffect(() => {
    // Risk caps for utilization subs (AccountPanel) + the limits row (system health).
    // Refetched when the controls modal closes (a saved cap shows immediately) AND on a book
    // switch (re-audit: the other book's caps must never render under this book's numbers).
    if (showControls) return;
    fetch(withBook("/api/control"))
      .then((r) => (r.ok ? r.json() : null))
      .then((j) => {
        const e = j?.editable || {};
        setCaps({
          stake: e.ctl_stake_usd?.value,
          maxOpen: e.ctl_max_concurrent?.value,
          deployedCap: e.ctl_total_deployed_cap_usd?.value,
          dailyLossCap: e.ctl_daily_loss_cap_usd?.value,
        });
      })
      .catch(() => {});
  }, [showControls, book]);

  useEffect(() => {
    // F41: both the initial HTTP fetch AND the WS push a full snapshot. Guard on the
    // monotonic meta.generated_at so a slow HTTP fetch resolving AFTER a fresher WS snapshot
    // can't clobber it with older data (ISO timestamps compare lexicographically).
    let lastGen = "";
    let alive = true;
    const onSnap = (s) => {
      if (!alive) return;
      // CRITICAL (audit reverify-3 F1): drop any snapshot from the OTHER book. A late fetch/frame
      // resolving after a book toggle would otherwise render e.g. the paper twin's positions under
      // LIVE badges — and a "close" click there routes REAL money. meta.book is stamped on every
      // REST + WS payload; a missing value means the live WS (legacy) → treat as "live".
      if ((s?.meta?.book || "live") !== book) return;
      const gen = s?.meta?.generated_at || "";
      if (gen && gen < lastGen) return;
      if (gen) lastGen = gen;
      setSnap(s);
      setUpdated(new Date());
    };
    fetchSnapshot(book).then(onSnap).catch(() => {});
    if (book === "paper") {
      // PAPER view: the WS pushes the LIVE book, so poll the paper snapshot instead.
      setFeed("paper");
      const iv = setInterval(() => fetchSnapshot("paper").then(onSnap).catch(() => {}), 4000);
      return () => {
        alive = false;
        clearInterval(iv);
      };
    }
    const disconnect = connectWS(onSnap, setFeed);
    return () => {
      alive = false;
      disconnect();
    };
  }, [book]);

  const meta = snap?.meta;
  const stats = snap?.stats;
  const asDesigned = stats?.as_designed;
  const dot = feed === "live" ? "bg-win" : feed === "down" ? "bg-loss" : "bg-muted";

  return (
    <div className="min-h-screen p-4 flex flex-col gap-3" key={book}>
      {/* Header */}
      <header className="flex items-start gap-3 px-1">
        <LogoGlyph />
        <LogoMark />
        <div className="min-w-0 pt-0.5">
          <div className="flex items-baseline gap-2.5">
            <span className="mepola-wm-sub leading-none">MEME · POWER · LAW</span>
          </div>
          <div className="text-[14px] text-muted mt-1.5 flex items-center gap-1.5 truncate">
            <span className="truncate">config #1 · a deliberate power-law tail-rider</span>
            <InfoHint text="The locked rule: buy a −50% dip from the signal price (within 48h) → hard stop at −30% until secured → at 3× sell 33% and remove the stop → then ride, selling 25% of the rest at 6/12/24/48× then ×3 → no re-entry → $3 fixed stake." />
          </div>
        </div>
        <div className="flex-1" />
        <div className="flex items-center gap-2 pt-1.5">
          <div
            className="mepola-badge flex items-center px-0 overflow-hidden"
            title="switch the machine you're viewing — LIVE trades real money; PAPER is the measurement twin (read-only view)"
          >
            {["live", "paper"].map((b) => (
              <button
                key={b}
                onClick={() => switchBook(b)}
                className={`px-2 py-0.5 text-[13px] font-bold tracking-[0.12em] uppercase transition-colors ${
                  book === b
                    ? b === "live"
                      ? "bg-live text-base"
                      : "bg-tail text-base"
                    : "text-muted hover:text-ink"
                }`}
              >
                {b}
              </button>
            ))}
          </div>
          <span
            className={`mepola-badge px-2 text-[13px] font-bold tracking-[0.12em] ${
              readOnly ? "text-tail" : "text-live"
            }`}
            title={
              readOnly
                ? "the paper machine: practice trades with simulated money + the uncapped measurement baseline"
                : undefined
            }
          >
            {readOnly ? "PAPER · PRACTICE" : (meta?.mode || "paper").toUpperCase()}
          </span>
          <span
            className={`mepola-badge px-2 text-[13px] font-bold tracking-[0.06em] ${
              asDesigned ? "text-win" : "text-live"
            }`}
          >
            {asDesigned ? "AS DESIGNED ✓" : "OFF EXPECTATION ⚠"}
          </span>
          <span className="mepola-badge px-2 text-[13px] text-muted flex items-center gap-1.5 uppercase tracking-[0.06em]">
            <span className={`w-1.5 h-1.5 ${dot} ${feed === "live" ? "pulse-dot" : ""}`} />
            feed:{feed || "…"}
          </span>
          {meta?.n_open_orders > 0 && (
            <span
              className="mepola-badge px-2 text-[13px] text-tail font-bold tracking-[0.06em]"
              title="resting manual orders in this book"
            >
              {meta.n_open_orders} ORDER{meta.n_open_orders > 1 ? "S" : ""}
            </span>
          )}
          {meta?.exec_pending > 0 && (
            <span
              className="mepola-badge px-2 text-[13px] text-live font-bold tracking-[0.06em] flex items-center gap-1.5"
              title="live swaps in flight"
            >
              <span className="w-1.5 h-1.5 bg-live pulse-dot" />
              {meta.exec_pending} EXEC
            </span>
          )}
          <span className="num text-[13px] text-muted/70">
            upd {updated ? updated.toLocaleTimeString([], { hour12: false }) : "—"}
            <span className="blink-cursor text-live ml-1">▊</span>
          </span>
          <button
            aria-label="controls"
            title="runtime controls"
            onClick={() => setShowControls(true)}
            className={`text-muted hover:text-ink transition p-1 -ml-0.5 ${readOnly ? "hidden" : ""}`}
          >
            <svg
              width="14"
              height="14"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.8"
              strokeLinecap="round"
              strokeLinejoin="round"
            >
              <circle cx="12" cy="12" r="3" />
              <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09a1.65 1.65 0 0 0-1-1.51 1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09a1.65 1.65 0 0 0 1.51-1 1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" />
            </svg>
          </button>
        </div>
      </header>

      {/* Row B: the hero dominates */}
      <div className="grid grid-cols-3 grid-rows-1 gap-3 h-[57vh]">
        <div className="col-span-2 min-w-0 panel p-3 flex flex-col overflow-hidden">
          <div className="px-1 pb-2">
            <div className="flex items-center gap-2.5">
              <span className="panel-title text-[15px] font-semibold text-ink shrink-0">
                power-law of every position
              </span>
              <InfoHint text="Every position ranked by return multiple — the one huge winner IS the strategy. Green = win, red = loss, lime = still live. The dashed lime line is the cumulative % of all gains; the ghost dashes are the ideal power-law curve." />
              <span className="ml-auto shrink-0">
                <ScopeToggle
                  value={heroScope}
                  onChange={setHeroScope}
                  options={["live", "seed", "all"]}
                />
              </span>
            </div>
          </div>
          <div className="flex-1 min-h-0">
            <PowerLawHero snapshot={snap} scope={heroScope} onSelect={setSel} />
          </div>
        </div>
        <div className="col-span-1 flex flex-col gap-3 overflow-auto">
          <AccountPanel
            account={snap?.account}
            caps={caps}
            book={book}
            wallet={
              meta?.wallet_sol != null
                ? { sol: meta.wallet_sol, usd: meta.wallet_usd, at: meta.wallet_at }
                : null
            }
          />
          <StatTiles stats={stats} />
          <div className="panel p-3 h-[21vh] shrink-0 flex flex-col">
            <div className="flex items-center gap-1.5 pb-1.5">
              <div className="tile-label">tail shape · log-log CCDF</div>
              <InfoHint text="How heavy the winning tail is. Each point: a return multiple (x) vs the share of trades that beat it (y), both on log axes. A straight line = a true power law; the shallower it falls, the fatter the tail." />
            </div>
            <div className="flex-1 min-h-0">
              <HillPanel snapshot={snap} />
            </div>
          </div>
        </div>
      </div>

      {/* Row C: equity + daily P&L + health. grid-rows-1 (=minmax(0,1fr)) pins the row to 27vh so a
          tall alerts list can't push the row taller and spill into the positions table below. */}
      <div className="grid grid-cols-3 grid-rows-1 gap-3 h-[27vh]">
        <div className="col-span-2 min-w-0 min-h-0 panel p-3 flex gap-3 overflow-hidden">
          <div className="flex-1 min-w-0 flex flex-col">
            <div className="px-1 pb-2">
              <div className="flex items-center gap-2.5">
                <span className="panel-title text-[15px] font-semibold text-ink">
                  {eqTab === "live"
                    ? readOnly
                      ? "paper account · measurement"
                      : "your live account"
                    : "backtest replay"}
                </span>
                <InfoHint
                  text={
                    eqTab === "live"
                      ? readOnly
                        ? "The paper measurement machine — $500 notional bankroll, takes every call uncapped. Not your money; the honest baseline live is judged against."
                        : "Anchored to the real burner wallet at go-live — every closed live trade moves this line."
                      : "The backtest evidence behind the strategy — not your money. $3-fixed vs 0.6%-fractional sizing on the seed corpus."
                  }
                />
                <span className="ml-auto flex items-center gap-1.5 shrink-0">
                  {eqTab === "live" ? (
                    <LegendPill c="#CBF14E" t={readOnly ? "paper balance" : "live balance"} />
                  ) : (
                    <>
                      <LegendPill c="#FF5147" t="seed $3-fixed" />
                      <LegendPill c="#3DDC84" t="seed 0.6%-frac" />
                    </>
                  )}
                  <ScopeToggle value={eqTab} onChange={setEqTab} options={["live", "backtest"]} />
                </span>
              </div>
            </div>
            <div className="flex-1 min-h-0">
              <EquityCurve snapshot={snap} mode={eqTab} />
            </div>
          </div>
          <div className="w-[230px] shrink-0 border-l border-edge/60 pl-3 flex flex-col">
            <div className="tile-label pb-1.5 pt-0.5 whitespace-nowrap" style={{ fontSize: 12 }}>
              daily p&amp;l · live realized
            </div>
            <div className="flex-1 min-h-0">
              <DailyPnl data={snap?.daily_pnl} />
            </div>
          </div>
        </div>
        <div className="col-span-1 min-h-0 overflow-hidden">
          <AlertsPanel alerts={snap?.alerts} meta={meta} signals={snap?.signals} caps={caps} />
        </div>
      </div>

      {/* Row D: positions + recent calls (sizes to content — collapses when empty) */}
      <PositionsTable
        positions={snap?.positions}
        signals={snap?.signals}
        flow={snap?.signal_flow}
        onSelect={setSel}
        book={book}
      />

      {/* Row E: trade history — what already finished, in plain terms */}
      <TradeHistory history={snap?.history} onSelect={setSel} />

      {/* Row F: strategy lab — forward shadow race. Mutations (new strategy / delete / re-measure)
          act on the LIVE book's challenger set only, so they're hidden in the paper view. */}
      <StrategyLab lab={snap?.lab} readOnly={readOnly} />

      {sel && <TokenModal mint={sel} onClose={() => setSel(null)} />}
      {showControls && !readOnly && <ControlsModal onClose={() => setShowControls(false)} />}
    </div>
  );
}
