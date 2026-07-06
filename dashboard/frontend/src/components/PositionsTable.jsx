import React, { useEffect, useState } from "react";
import DiveGauge, { feedTrails, watcherRowClass } from "./DiveGauge";
import { igniteClass } from "../ignite";
import InfoHint from "./InfoHint";
import AddTokenModal from "./AddTokenModal";

const BADGE = {
  WATCHING: "text-muted",
  ENTERED: "text-live",
  SECURED: "text-tail",
  RIDING: "text-win",
};

function StateBadge({ state }) {
  return (
    <span
      className={`mepola-badge inline-block px-1 text-[13px] font-bold tracking-[0.08em] ${
        BADGE[state] || "text-ink"
      }`}
    >
      {state}
    </span>
  );
}

const fmtAge = (h) =>
  h == null ? "—" : h < 1 ? Math.max(1, Math.round(h * 60)) + "m" : h < 48 ? h.toFixed(1) + "h" : (h / 24).toFixed(1) + "d";

// Signed money, matching the other money cells: '$' + U+2212 for negatives (F45).
const fmtSignedUsd = (v) =>
  v == null ? "—" : (v > 0 ? "+$" : v < 0 ? "−$" : "$") + Math.abs(v).toFixed(2);

// Pipeline strip: is the signal flow alive?
function Flow({ label, value, tone = "text-ink" }) {
  return (
    <span className="flex items-baseline gap-1.5 whitespace-nowrap">
      <span className="text-[12px] uppercase tracking-[0.12em] text-muted/70 font-semibold">
        {label}
      </span>
      <span className={`num text-[14px] font-semibold ${tone}`}>{value}</span>
    </span>
  );
}

function PipelineStrip({ flow }) {
  if (!flow) return null;
  return (
    <div className="ml-auto flex items-center gap-3 divide-x divide-edge/80 shrink-0">
      <span className="text-[12px] uppercase tracking-[0.14em] text-muted/60 font-semibold pr-0.5">
        signal flow
      </span>
      <span className="pl-3 flex items-center gap-3">
        <Flow label="calls 24h" value={flow.calls_24h} />
        <Flow label="7d" value={flow.calls_7d} />
      </span>
      <span className="pl-3">
        <Flow
          label="entry rate"
          value={flow.entry_rate_pct != null ? `${flow.entry_rate_pct}%` : "—"}
          tone="text-win"
        />
      </span>
      <span className="pl-3">
        <Flow
          label="watching now"
          value={flow.watching_now}
          tone={flow.watching_now > 0 ? "text-live" : "text-ink"}
        />
      </span>
      <span className="pl-3">
        <Flow
          label="expired no-dip"
          value={flow.expired_no_dip_pct != null ? `${flow.expired_no_dip_pct}%` : "—"}
          tone="text-muted"
        />
      </span>
    </div>
  );
}

export default function PositionsTable({ positions, signals, flow, onSelect, book = "live" }) {
  const rows = positions || [];
  const paper = book === "paper";
  const [showAdd, setShowAdd] = useState(false);
  // feed the client-side price trails behind each watcher's dive gauge
  useEffect(() => {
    feedTrails(positions);
  }, [positions]);
  return (
    <div className="panel p-3 h-full flex flex-col">
      {showAdd && <AddTokenModal onClose={() => setShowAdd(false)} />}
      <div className="flex items-center gap-3 flex-wrap mb-2">
        <div className="flex items-center gap-1.5">
          <div className="tile-label">
            {paper ? "paper positions" : "live positions"} {rows.length ? `(${rows.length})` : ""}
          </div>
          <InfoHint
            text={
              paper
                ? "Everything the paper machine is trading — takes every call uncapped at a $3 notional stake, and it's your PRACTICE desk: [EXTERNAL INPUT] and every token-view action work here with simulated money."
                : "Everything the algo is trading — tokens it's watching for the −50% dip, plus what it holds. Add your own token or buy one now with [EXTERNAL INPUT]; override any position from its token view."
            }
          />
          <button
            onClick={() => setShowAdd(true)}
            title={paper ? "practice: add a token or buy one with simulated money" : "add a token (watchlist) or buy one directly"}
            className="ml-1 text-[12px] font-bold uppercase tracking-[0.08em] px-1.5 py-0.5 border border-tail/50 text-tail hover:bg-tail hover:text-base bg-black/30 transition-colors"
          >
            [ external input ]
          </button>
        </div>
        <PipelineStrip flow={flow} />
      </div>
      {rows.length === 0 ? (
        <div className="flex items-center gap-4 text-[15px] text-muted px-1 py-1.5 leading-relaxed">
          <pre className="text-[13px] leading-[1.05] text-live/60 shrink-0 m-0 select-none" aria-hidden="true">
            {`((( · )))
    │
   ╱│╲
▔▔▔▔▔▔▔▔▔`}
          </pre>
          <span>
            NO OPEN POSITIONS — SCANNING▊ The engine watches every first-call for a −50% dip;
            when one fills it appears here (and pulses lime on the chart). Recent calls below.
          </span>
        </div>
      ) : (
        <div className="overflow-auto">
          <table className="w-full text-xs">
            <thead>
              <tr className="text-left text-[13px] uppercase tracking-[0.14em] text-muted">
                <th className="py-1.5 font-semibold">ticker</th>
                <th className="font-semibold">state</th>
                <th className="text-right font-semibold">age</th>
                <th className="text-right font-semibold">entry</th>
                <th className="text-right font-semibold">mult</th>
                <th className="text-right font-semibold">peak</th>
                <th className="text-right font-semibold">next rung</th>
                <th className="text-right font-semibold">stop</th>
                <th className="text-right font-semibold" title="open bag marked at the latest price — realized only when it closes">
                  P&L · marked
                </th>
              </tr>
            </thead>
            <tbody className="num">
              {rows.map((p) => {
                const watching = p.state === "WATCHING";
                const unsecured = p.state === "ENTERED";
                return (
                  <tr
                    key={p.mint}
                    className={`border-t border-edge/60 cursor-pointer hover:bg-live/[0.05] ${
                      watching ? watcherRowClass(p) : ""
                    }`}
                    onClick={() => onSelect?.(p.mint)}
                  >
                    <td className="py-1.5 font-semibold text-ink">
                      <span className="inline-flex items-center gap-1.5">
                        {p.ticker || p.mint.slice(0, 4)}
                        {p.controller === "manual" && (
                          <span
                            className="mepola-badge px-1 text-[11px] text-tail font-bold tracking-[0.06em]"
                            title="you're driving this one (override) — the algo isn't managing it. Manage it from its token view."
                          >
                            you
                          </span>
                        )}
                      </span>
                    </td>
                    <td>
                      <StateBadge state={p.state} />
                    </td>
                    <td className="text-right text-muted">{fmtAge(p.age_h)}</td>
                    {watching ? (
                      <td colSpan={5} className="pl-4 pr-2">
                        <DiveGauge p={p} />
                      </td>
                    ) : (
                      <>
                        <td className="text-right">
                          {p.entry_price ? p.entry_price.toPrecision(3) : "—"}
                        </td>
                        <td className="text-right">
                          {p.current_multiple ? (
                            <>
                              <span className={igniteClass(p.current_multiple)}>
                                {p.current_multiple.toFixed(2) + "x"}
                              </span>
                              {p.remaining_frac != null && p.remaining_frac < 1 && (
                                <span
                                  className="text-muted/60"
                                  title="bag remaining after take-profit sells"
                                >
                                  {" "}
                                  · {Math.round(p.remaining_frac * 100)}%
                                </span>
                              )}
                            </>
                          ) : (
                            "—"
                          )}
                        </td>
                        <td className="text-right text-muted">
                          {p.peak_multiple ? p.peak_multiple.toFixed(2) + "x" : "—"}
                        </td>
                        <td className="text-right text-muted">
                          {p.next_rung_mult ? (
                            <>
                              {p.next_rung_mult}x
                              {p.dist_to_next_rung_pct != null && (
                                <span className="text-muted/60">
                                  {" "}
                                  (+{Math.round(p.dist_to_next_rung_pct)}%)
                                </span>
                              )}
                            </>
                          ) : (
                            "—"
                          )}
                        </td>
                        <td className="text-right">
                          {unsecured ? (
                            p.dist_to_stop_pct != null ? (
                              <span className="text-loss">
                                −{Math.abs(Math.round(p.dist_to_stop_pct))}%
                              </span>
                            ) : (
                              "—"
                            )
                          ) : (
                            <span className="text-muted/50">removed</span>
                          )}
                        </td>
                      </>
                    )}
                    <td
                      className={`text-right ${
                        watching || p.realized_pnl_usd == null || p.realized_pnl_usd === 0
                          ? "text-muted/50"       // F45: flat/null is neutral, not green 0.00
                          : p.realized_pnl_usd > 0
                          ? "text-win"
                          : "text-loss"
                      }`}
                    >
                      {watching ? "—" : fmtSignedUsd(p.realized_pnl_usd)}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      {signals && signals.length > 0 && (
        <div className="mt-3 pb-1">
          <div className="border-t border-edge/60 mb-2.5" aria-hidden="true" />
          <div className="tile-label mb-2">recent calls</div>
          <div className="flex flex-wrap gap-1.5">
            {signals.slice(0, 14).map((s, i) => {
              const rejected = s.accepted === 0 || s.accepted === false;
              const when = s.ts ? String(s.ts).slice(0, 16).replace("T", " ") : "";
              return (
                <button
                  key={i}
                  onClick={() => s.mint && onSelect?.(s.mint)}
                  title={rejected ? `${when} · rejected: ${s.reject_reason || "?"}` : when}
                  className={`mepola-badge num text-[13px] px-1 transition-colors cursor-pointer ${
                    rejected ? "text-loss/70 hover:text-loss" : "text-muted hover:text-ink"
                  }`}
                >
                  {s.ticker || s.mint?.slice(0, 4)}
                  {rejected ? " ✕" : ""}
                </button>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}
