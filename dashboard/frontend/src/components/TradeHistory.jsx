import React, { useEffect, useState } from "react";
import { igniteClass } from "../ignite";
import { withBook } from "../api";
import InfoHint from "./InfoHint";

// Completed trades + watchers that never entered — the "what already happened" panel.
// LIVE tab = the paper account's own history (streams via the snapshot);
// SEED tab = the backtest replay's history (fetched once, paged via /api/history).

const PAGE = 100;

const OUTCOME = {
  stopped: { text: "STOPPED", cls: "text-loss" },
  sold_out: { text: "SOLD OUT", cls: "text-win" },
  rode_to_horizon: { text: "RODE", cls: "text-win" },
  time_stop: { text: "TIME STOP", cls: "text-muted" },
};
const EXPIRED = { text: "EXPIRED", cls: "text-muted" };

function OutcomeBadge({ r }) {
  const o =
    r.kind === "expired"
      ? EXPIRED
      : OUTCOME[r.close_reason] || {
          text: (r.close_reason || "closed").replace(/_/g, " ").toUpperCase(),
          cls: "text-ink",
        };
  return (
    <span
      className={`mepola-badge inline-block px-1 text-[13px] font-bold tracking-[0.08em] whitespace-nowrap ${o.cls}`}
    >
      {o.text}
    </span>
  );
}

// STREAM action -> tone. BUY glows lime (money out), sells read as win/loss instantly.
const ACTION_CLS = {
  CALL: "text-muted",
  BUY: "text-live",
  "TAKE PROFIT": "text-win",
  "RIDE SELL": "text-win",
  CUT: "text-loss",
  "FINAL SELL": "text-ink",
  EXPIRED: "text-muted",
};

const fmtWhen = (iso) => {
  if (!iso) return "—";
  const d = new Date(iso);
  return (
    d.toLocaleDateString("en-US", { month: "short", day: "numeric" }) +
    " " +
    d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false })
  );
};
const fmtFdv = (v) =>
  v == null
    ? "—"
    : v >= 1e9
    ? "$" + (v / 1e9).toFixed(2) + "B"
    : v >= 1e6
    ? "$" + (v / 1e6).toFixed(2) + "M"
    : "$" + Math.round(v / 1e3) + "K";
const fmtFrac = (f) => (f == null ? "—" : Math.round(f * 100) + "%");
const fmtHeld = (h) =>
  h == null ? "—" : h < 1 ? Math.max(1, Math.round(h * 60)) + "m" : h < 48 ? h.toFixed(1) + "h" : (h / 24).toFixed(1) + "d";
const fmtMult = (m) => (m == null ? "—" : m.toFixed(2) + "x");
const fmtPnl = (v) => (v == null ? "—" : (v > 0 ? "+$" : v < 0 ? "−$" : "$") + Math.abs(v).toFixed(2));

async function fetchRows(scope, limit) {
  const r = await fetch(withBook(`/api/history?scope=${scope}&limit=${limit}`));
  if (!r.ok) throw new Error("history fetch failed");
  const j = await r.json();
  return j.rows || [];
}

// The raw execution feed: EVERY order the machine placed, one row per event.
function StreamTable({ rows, onSelect }) {
  return (
    <div className="overflow-auto max-h-[42vh]">
      <table className="w-full text-xs">
        <thead className="sticky top-0 bg-[#080E06]">
          <tr className="text-left text-[13px] uppercase tracking-[0.14em] text-muted">
            <th className="py-1.5 font-semibold">time</th>
            <th className="font-semibold">ticker</th>
            <th className="font-semibold">execution</th>
            <th className="text-right font-semibold" title="fully-diluted valuation at the executed price">
              fdv @ exec
            </th>
            <th className="text-right font-semibold" title="fraction of the original position">
              size
            </th>
            <th className="text-right font-semibold">value</th>
            <th className="text-right font-semibold" title="sells only: proceeds minus that fraction's cost">
              p&amp;l
            </th>
          </tr>
        </thead>
        <tbody className="num">
          {rows.map((r) => {
            const rung = r.rung_mult ? ` @${Number(r.rung_mult).toFixed(0)}×` : "";
            const passive = r.action === "CALL" || r.action === "EXPIRED";
            return (
              <tr
                key={r.id}
                className={`border-t border-edge/60 cursor-pointer hover:bg-live/[0.05] ${
                  passive ? "opacity-50" : ""
                }`}
                onClick={() => onSelect?.(r.mint)}
              >
                <td className="py-1.5 text-muted whitespace-nowrap">{fmtWhen(r.ts)}</td>
                <td className="font-semibold text-ink pr-2">{r.ticker}</td>
                <td className="pr-2">
                  <span
                    className={`mepola-badge inline-block px-1 text-[13px] font-bold tracking-[0.08em] whitespace-nowrap ${
                      ACTION_CLS[r.action] || "text-ink"
                    }`}
                  >
                    {r.action}
                    {rung}
                  </span>
                </td>
                <td className="text-right text-muted">{fmtFdv(r.fdv_usd)}</td>
                <td className="text-right text-muted">
                  {r.action === "BUY" ? "100%" : passive ? "—" : fmtFrac(r.frac)}
                </td>
                <td className="text-right text-ink">
                  {r.value_usd != null ? "$" + r.value_usd.toFixed(2) : "—"}
                </td>
                <td
                  className={`text-right font-semibold ${
                    (r.pnl_usd || 0) > 0
                      ? "text-win"
                      : (r.pnl_usd || 0) < 0
                      ? "text-loss"
                      : "text-muted"
                  }`}
                >
                  {fmtPnl(r.pnl_usd)}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

export default function TradeHistory({ history, onSelect }) {
  const [tab, setTab] = useState("stream");
  // LIVE rows stream in via the snapshot (first 100); "load more" swaps to fetched rows.
  const [fetched, setFetched] = useState({ live: null, seed: null });
  const [stream, setStream] = useState(null); // null = loading; [] = honestly empty
  const [streamErr, setStreamErr] = useState(false);
  const [streamScope, setStreamScope] = useState("live");
  const [streamLimit, setStreamLimit] = useState(120);
  const [loading, setLoading] = useState(false);

  const liveCount = history?.live_count ?? 0;
  const seedCount = history?.seed_count ?? 0;
  const snapLive = history?.rows || [];
  const rows =
    tab === "stream" ? stream || [] : tab === "live" ? fetched.live || snapLive : fetched.seed || [];
  const total = tab === "live" ? liveCount : seedCount;

  useEffect(() => {
    // SEED history isn't in the snapshot — fetch it the first time the tab opens.
    if (tab === "seed" && fetched.seed == null && !loading) {
      setLoading(true);
      fetchRows("seed", PAGE)
        .then((r) => setFetched((f) => ({ ...f, seed: r })))
        .catch(() => {})
        .finally(() => setLoading(false));
    }
  }, [tab, fetched.seed, loading]);

  useEffect(() => {
    // STREAM refreshes on its own clock while visible (events land within seconds).
    if (tab !== "stream") return undefined;
    let alive = true;
    const pull = () =>
      fetch(withBook(`/api/stream?scope=${streamScope}&limit=${streamLimit}`))
        .then((r) => (r.ok ? r.json() : Promise.reject()))
        .then((j) => {
          if (!alive) return;
          setStream(j.rows || []);
          setStreamErr(false);
        })
        .catch(() => alive && setStreamErr(true));
    pull();
    const t = setInterval(pull, 8000);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, [tab, streamScope, streamLimit]);

  const loadMore = () => {
    if (loading) return;
    setLoading(true);
    fetchRows(tab, rows.length + PAGE)
      .then((r) => setFetched((f) => ({ ...f, [tab]: r })))
      .catch(() => {})
      .finally(() => setLoading(false));
  };

  return (
    <div className="panel p-3 flex flex-col">
      <div className="flex items-center gap-3 flex-wrap mb-2">
        <div className="flex items-center gap-1.5">
          <div className="tile-label">trade history</div>
          <InfoHint
            text={
              tab === "stream"
                ? "Every execution in real time — calls, buys, take-profits, cuts — as the machine places them, with the FDV at each executed price."
                : tab === "live"
                ? "Every finished call of your paper account — what closed, why, and what it made."
                : "The backtest replay's finished calls — the same rules run over the seed corpus (not your money)."
            }
          />
        </div>
        {tab === "stream" && (
          <div className="flex items-center gap-0.5 shrink-0">
            {["live", "all"].map((k) => (
              <button
                key={k}
                onClick={() => setStreamScope(k)}
                className={`px-1.5 py-[2px] text-[11px] font-bold tracking-[0.12em] uppercase num border ${
                  streamScope === k
                    ? "bg-tail text-base border-tail"
                    : "bg-black/30 border-edge text-muted hover:text-ink"
                }`}
              >
                {k}
              </button>
            ))}
          </div>
        )}
        <div className="ml-auto flex items-center bg-live/[0.05] border border-edge p-0.5 gap-0.5 shrink-0">
          {[
            ["stream", "STREAM"],
            ["live", `CLOSED (${liveCount})`],
            ["seed", `SEED (${seedCount})`],
          ].map(([k, label]) => (
            <button
              key={k}
              onClick={() => setTab(k)}
              className={`px-2 py-[3px] text-[12px] font-bold tracking-[0.14em] uppercase num ${
                tab === k ? "bg-tail text-base" : "text-muted hover:text-ink hover:bg-live/[0.06]"
              }`}
            >
              {label}
            </button>
          ))}
        </div>
      </div>

      {tab === "stream" && streamErr && (
        <div className="text-[13px] text-live mb-1.5">
          stream unavailable — the server may be restarting; retrying every 8s
          {stream?.length ? " (showing the last data received)" : ""}
        </div>
      )}
      {tab === "stream" && rows.length > 0 ? (
        <>
          <StreamTable rows={rows} onSelect={onSelect} />
          {rows.length >= streamLimit && streamLimit < 500 && (
            <button
              onClick={() => setStreamLimit(Math.min(500, streamLimit + 120))}
              className="mt-2 mx-auto text-[13px] font-semibold px-3 py-1 border bg-black/30 border-edge text-muted hover:bg-ink hover:text-base hover:border-ink uppercase tracking-[0.08em]"
            >
              [ LOAD MORE — {rows.length} SHOWN ]
            </button>
          )}
        </>
      ) : rows.length === 0 ? (
        <div className="flex items-center gap-2 text-[15px] text-muted px-1 py-1.5 leading-relaxed">
          <span className="w-1.5 h-1.5 rounded-full bg-live/70 pulse-dot shrink-0" />
          {loading ? (
            <span>
              <span className="ascii-spinner mr-1.5 text-live" />
              LOADING…
            </span>
          ) : tab === "stream" ? (
            streamErr ? (
              "STREAM UNAVAILABLE▊ retrying…"
            ) : stream === null ? (
              <span>
                <span className="ascii-spinner mr-1.5 text-live" />
                LOADING…
              </span>
            ) : (
              "NO EXECUTIONS YET▊ every order the machine places prints here live."
            )
          ) : tab === "live" ? (
            "NO COMPLETED TRADES▊ history lands here as watchers resolve."
          ) : (
            "NO SEED HISTORY▊"
          )}
        </div>
      ) : (
        <div className="overflow-auto max-h-[42vh]">
          <table className="w-full text-xs">
            <thead className="sticky top-0 bg-[#080E06]">
              <tr className="text-left text-[13px] uppercase tracking-[0.14em] text-muted">
                <th className="py-1.5 font-semibold">closed</th>
                <th className="font-semibold">ticker</th>
                <th className="font-semibold">outcome</th>
                <th className="text-right font-semibold">entry</th>
                <th className="text-right font-semibold">multiple</th>
                <th className="text-right font-semibold">p&amp;l</th>
                <th className="text-right font-semibold">held</th>
              </tr>
            </thead>
            <tbody className="num">
              {rows.map((r, i) => {
                const expired = r.kind === "expired";
                return (
                  <tr
                    key={r.mint + (r.closed_at || i)}
                    className={`border-t border-edge/60 cursor-pointer hover:bg-live/[0.05] ${
                      expired ? "opacity-50" : ""
                    }`}
                    onClick={() => onSelect?.(r.mint)}
                  >
                    <td className="py-1.5 text-muted whitespace-nowrap">{fmtWhen(r.closed_at)}</td>
                    <td className="font-semibold text-ink pr-2">{r.ticker}</td>
                    <td className="pr-2">
                      <OutcomeBadge r={r} />
                    </td>
                    {expired ? (
                      <td colSpan={4} className="text-right text-muted font-sans">
                        never entered — no −50% dip in 48h
                      </td>
                    ) : (
                      <>
                        <td className="text-right text-muted">
                          {r.entry_price ? Number(r.entry_price).toPrecision(3) : "—"}
                        </td>
                        <td
                          className={`text-right font-semibold ${
                            (r.realized_multiple || 0) >= 1 ? "text-win" : "text-loss"
                          } ${igniteClass(r.realized_multiple)}`}
                        >
                          {fmtMult(r.realized_multiple)}
                        </td>
                        <td
                          className={`text-right ${
                            (r.pnl_usd || 0) > 0
                              ? "text-win"
                              : (r.pnl_usd || 0) < 0
                              ? "text-loss"
                              : "text-muted"
                          }`}
                        >
                          {fmtPnl(r.pnl_usd)}
                        </td>
                        <td className="text-right text-muted">{fmtHeld(r.held_hours)}</td>
                      </>
                    )}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {tab !== "stream" && rows.length > 0 && rows.length < total && (
        <button
          onClick={loadMore}
          disabled={loading}
          className="mt-2 mx-auto text-[13px] font-semibold px-3 py-1 border bg-black/30 border-edge text-muted hover:bg-ink hover:text-base hover:border-ink disabled:opacity-50 uppercase tracking-[0.08em]"
        >
          {loading ? (
            <span>
              <span className="ascii-spinner mr-1.5" />
              LOADING…
            </span>
          ) : (
            `[ LOAD MORE — ${rows.length} OF ${total} ]`
          )}
        </button>
      )}
    </div>
  );
}
