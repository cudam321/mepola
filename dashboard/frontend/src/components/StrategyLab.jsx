import React, { useEffect, useState } from "react";
import { ConfigDetail, StrategyBuilder } from "./LabModals";
import InfoHint from "./InfoHint";

// Forward shadow race: C1..C18 + user-added X* configs replayed against the SAME live
// ticks. Forward evidence only — no lookahead. Promotion requires gates + human approval.

// Fallback labels for old servers without lab.meta; the server's meta wins.
const FALLBACK_LABELS = {
  C1: "champion #1",
  C2: "#1 + re-entry",
  C3: "deep stop −50%",
  C4: "no stop",
  C5: "shallow dip −30%",
  C6: "early secure 2×/50",
  C7: "no dip (chase)",
  C8: "fast secure 1.5×",
  C9: "soft everything",
  C10: "diamond hand",
};

// Controls chosen to DOCUMENT a failure mode (not to win): C7 chases with no dip, C10 never
// secures (diamond hand). Flagged so a lucky forward run can't read as a recommended strategy.
const CONTROL_IDS = new Set(["C7", "C10"]);

const FAMILY_ORDER = ["core", "entry", "exit", "gate", "custom"];
const FAMILY_TITLE = {
  core: "core",
  entry: "entry variants",
  exit: "exit variants",
  gate: "gated",
  custom: "yours",
};

// {C1:{label,family}, ...} — from the server when available, else the fallback.
function configMeta(lab) {
  if (lab?.meta && Object.keys(lab.meta).length) return lab.meta;
  return Object.fromEntries(
    Object.entries(FALLBACK_LABELS).map(([id, label]) => [id, { label, family: "core" }])
  );
}

// [{family, title, ids:[...] }, ...] in a stable, numeric C-order per family.
function familyGroups(meta) {
  const byFam = {};
  Object.keys(meta)
    .sort((a, b) => parseInt(a.slice(1), 10) - parseInt(b.slice(1), 10))
    .forEach((id) => {
      const fam = meta[id]?.family || "core";
      (byFam[fam] = byFam[fam] || []).push(id);
    });
  const known = FAMILY_ORDER.filter((f) => byFam[f]);
  const extra = Object.keys(byFam).filter((f) => !FAMILY_ORDER.includes(f));
  return [...known, ...extra].map((f) => ({
    family: f,
    title: FAMILY_TITLE[f] || f,
    ids: byFam[f],
  }));
}

const signed = (v) => (v > 0 ? "+$" : v < 0 ? "−$" : "$") + Math.abs(v).toFixed(2);

async function postControl(key, value) {
  const r = await fetch("/api/control", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ key, value }),
  });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || `request failed (${r.status})`);
  return j;
}

function cfgStr(cfg) {
  if (!cfg) return "—";
  const parts = [];
  if (cfg.dip != null) parts.push(`dip −${Math.round(cfg.dip * 100)}%`);
  if (cfg.sl != null) parts.push(cfg.sl ? `stop ${Math.round((1 - cfg.sl) * 100)}%` : "no stop");
  if (cfg.ftp != null) parts.push(`secure ${cfg.ftp}×/${Math.round((cfg.fsell || 0) * 100)}%`);
  parts.push(cfg.reentry ? "re-entry" : "no re-entry");
  return parts.join(" · ");
}

// ASCII spinner: | / - \ cycling via CSS steps — no SVG in a terminal
function Spinner() {
  return <span className="ascii-spinner shrink-0" aria-hidden="true" />;
}

// Subtle progress line for an in-flight run, e.g. "pricing 240/400 · started 12m ago".
// All fields are optional (parallel backend workstream) — degrade gracefully.
function progressLine(lr) {
  if (!lr || lr.status !== "running") return "running…";
  const parts = [];
  if (lr.priced != null && lr.total != null) parts.push(`${lr.phase || "pricing"} ${lr.priced}/${lr.total}`);
  else if (lr.phase) parts.push(String(lr.phase));
  const t = lr.started_at ? Date.parse(lr.started_at) : NaN;
  if (!Number.isNaN(t)) {
    const m = Math.round((Date.now() - t) / 60000);
    parts.push(m < 1 ? "started <1m ago" : `started ${m}m ago`);
  }
  return parts.length ? parts.join(" · ") : "running…";
}

export default function StrategyLab({ lab, readOnly = false }) {
  const configs = lab?.configs || {};
  const hasRows = Object.keys(configs).length > 0;
  const champion = lab?.champion || "C1";
  const meta = configMeta(lab);
  const groups = familyGroups(meta);
  // Server truth: research_running while a run is in flight; last_research may be
  // the running record ({status:'running', phase, priced, total, started_at}) or
  // the last terminal verdict ({status:'ok'|..., ts, n_tokens, recommendation}).
  const running = !!lab?.research_running;
  const lr = lab?.last_research;
  const lrTerminal = lr && lr.status !== "running" ? lr : null;

  const [busy, setBusy] = useState(false); // requested, waiting for the server to pick it up
  const [stale, setStale] = useState(false); // 30s passed with no server ack
  const [err, setErr] = useState(null);
  const [detailId, setDetailId] = useState(null); // open config drill-down
  const [builder, setBuilder] = useState(null); // null | {prefill: params|null}
  useEffect(() => {
    if (running) {
      setBusy(false); // server confirmed the run started — server state takes over
      setStale(false);
    }
  }, [running]);
  // Never an infinite local spinner: if the server hasn't acked within 30s,
  // release the button and say so.
  useEffect(() => {
    if (!busy) return;
    const t = setTimeout(() => {
      setBusy(false);
      setStale(true);
    }, 30000);
    return () => clearTimeout(t);
  }, [busy]);

  const remeasure = async () => {
    setErr(null);
    setStale(false);
    setBusy(true);
    try {
      await postControl("research_requested", "1");
    } catch (e) {
      setErr(e.message);
      setBusy(false);
    }
  };

  const spinning = running || busy;

  return (
    <div className="panel p-3 flex gap-4">
      {/* Left: the race table */}
      <div className="flex-1 min-w-0 flex flex-col">
        <div className="px-1 pb-2">
          <div className="flex items-center gap-1.5">
            <span className="panel-title text-[15px] font-semibold text-ink">
              strategy lab — forward shadow race
            </span>
            <InfoHint text="Rival strategies racing on the same live data — evidence, not opinions. Every config sees the same live ticks (no lookahead); promotion needs the gates AND your approval. Click a row for its rules, open riders and every closed leg." />
            {!readOnly && (
              <button
                onClick={() => setBuilder({ prefill: null })}
                className="ml-auto shrink-0 text-[12px] font-bold uppercase tracking-[0.1em] px-2 py-1 border bg-black/30 border-edge text-muted hover:bg-ink hover:text-base hover:border-ink"
              >
                [ + NEW STRATEGY ]
              </button>
            )}
          </div>
        </div>
        {!hasRows ? (
          <div className="flex-1 flex flex-col justify-center gap-2.5 px-1 py-4">
            <div className="flex items-center gap-2 text-[15px] text-muted">
              <span className="w-1.5 h-1.5 rounded-full bg-tail/70 pulse-dot shrink-0" />
              SHADOW RACE IDLE — starts with the next live call▊
            </div>
            {groups.map((g) => (
              <div key={g.family} className="flex items-center flex-wrap gap-1.5">
                <span className="text-[12px] uppercase tracking-[0.14em] text-muted/60 font-semibold w-[92px] shrink-0">
                  {g.title}
                </span>
                {g.ids.map((id) => (
                  <span
                    key={id}
                    className={`num text-[13px] px-1.5 py-0.5 border ${
                      id === champion
                        ? "bg-tail/15 border-tail/40 text-tail"
                        : "bg-black/30 border-edge/80 text-muted"
                    }`}
                  >
                    [{id}] {meta[id]?.label}
                  </span>
                ))}
              </div>
            ))}
          </div>
        ) : (
          <div className="overflow-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="text-left text-[13px] uppercase tracking-[0.14em] text-muted">
                  <th className="py-1.5 font-semibold">config</th>
                  <th className="text-right font-semibold">fwd trades</th>
                  <th className="text-right font-semibold">open</th>
                  <th className="text-right font-semibold">win%</th>
                  <th className="text-right font-semibold">mean</th>
                  <th className="text-right font-semibold">drop-top1</th>
                  <th className="text-right font-semibold" title="closed trades only">realized</th>
                  <th className="text-right font-semibold" title="open positions marked at the latest price">open p&l</th>
                  <th className="text-right font-semibold" title="realized + open at a phantom $3/trade — equals the account balance only while the live stake is $3">total</th>
                </tr>
              </thead>
              <tbody className="num">
                {groups
                  .map((g) => ({ ...g, ids: g.ids.filter((id) => configs[id]) }))
                  .filter((g) => g.ids.length)
                  .map((g) => (
                    <React.Fragment key={g.family}>
                      <tr>
                        <td
                          colSpan={9}
                          className="pt-2 pb-1 text-[12px] uppercase tracking-[0.16em] text-muted/60 font-semibold font-sans"
                        >
                          {g.title}
                        </td>
                      </tr>
                      {g.ids.map((id) => {
                        const c = configs[id];
                        const isChamp = id === champion;
                        return (
                          <tr
                            key={id}
                            onClick={() => setDetailId(id)}
                            className={`border-t border-edge/60 cursor-pointer hover:bg-live/[0.05] ${
                              isChamp ? "bg-tail/[0.07]" : ""
                            }`}
                          >
                            <td className="py-1.5 pr-2">
                              <span
                                className={`font-semibold ${isChamp ? "text-tail" : "text-ink"}`}
                              >
                                {id}
                              </span>
                              <span className="text-muted ml-2 font-sans">{meta[id]?.label}</span>
                              {isChamp && (
                                <span className="mepola-badge ml-2 px-1 text-tail text-[12px] font-bold tracking-[0.12em] font-sans">
                                  CHAMPION
                                </span>
                              )}
                              {CONTROL_IDS.has(id) && (
                                <span
                                  className="mepola-badge ml-2 px-1 text-muted/70 text-[11px] font-bold tracking-[0.12em] font-sans"
                                  title="a control chosen to DOCUMENT a failure mode, not to win"
                                >
                                  CONTROL
                                </span>
                              )}
                            </td>
                            <td className="text-right">{c.n_trades}</td>
                            <td className="text-right text-muted">{c.n_open}</td>
                            <td className="text-right">
                              {c.win_rate != null ? (c.win_rate * 100).toFixed(0) + "%" : "—"}
                            </td>
                            <td
                              className={`text-right ${
                                c.mean == null
                                  ? "text-muted"
                                  : c.mean >= 1 && (c.n_trades ?? 0) >= 10
                                  ? "text-win"      // F46: a real edge needs a sample, not one lucky tail
                                  : c.mean >= 1
                                  ? "text-muted"    // n<10: green would sell a non-repeatable win as a strategy
                                  : "text-loss"
                              }`}
                              title={
                                c.mean != null && (c.n_trades ?? 0) < 10
                                  ? "small forward sample — not enough trades to judge"
                                  : undefined
                              }
                            >
                              {c.mean != null ? c.mean.toFixed(2) + "x" : "—"}
                            </td>
                            <td className="text-right text-muted">
                              {c.drop_top1_mean != null && (c.n_trades ?? 0) >= 2
                                ? c.drop_top1_mean.toFixed(2) + "x"
                                : "—"}
                            </td>
                            <td
                              className={`text-right ${
                                (c.sum_pnl_at_3usd || 0) >= 0 ? "text-win" : "text-loss"
                              }`}
                            >
                              {c.sum_pnl_at_3usd != null ? signed(c.sum_pnl_at_3usd) : "—"}
                            </td>
                            <td className="text-right text-muted">
                              {c.open_pnl_at_3usd != null && c.open_pnl_at_3usd !== 0
                                ? signed(c.open_pnl_at_3usd)
                                : "—"}
                            </td>
                            <td
                              className={`text-right font-semibold ${
                                (c.total_pnl_at_3usd ?? c.sum_pnl_at_3usd ?? 0) >= 0
                                  ? "text-win"
                                  : "text-loss"
                              }`}
                            >
                              {signed(c.total_pnl_at_3usd ?? c.sum_pnl_at_3usd ?? 0)}
                            </td>
                          </tr>
                        );
                      })}
                    </React.Fragment>
                  ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* Right: last re-measurement + trigger */}
      <div className="w-[280px] shrink-0 border-l border-edge/60 pl-4 flex flex-col">
        <div className="tile-label mb-2">last re-measurement</div>
        {running ? (
          <div className="text-[14px] text-muted leading-relaxed">
            <span className="flex items-center gap-2 text-tail">
              <Spinner /> re-measurement in flight
            </span>
            <div className="num mt-1.5">{progressLine(lr)}</div>
          </div>
        ) : lrTerminal ? (
          <div className="space-y-1.5 text-[14px]">
            <div className="flex justify-between gap-2">
              <span className="text-muted">verdict</span>
              <span className="num text-ink">
                {lrTerminal.ts ? String(lrTerminal.ts).slice(0, 16).replace("T", " ") : "—"}
              </span>
            </div>
            <div className="flex justify-between gap-2">
              <span className="text-muted">tokens re-priced</span>
              <span className="num text-ink">{lrTerminal.n_tokens ?? "—"}</span>
            </div>
            <div className="flex justify-between gap-2">
              <span className="text-muted">status</span>
              <span className={`num ${lrTerminal.status === "ok" ? "text-win" : "text-live"}`}>
                {lrTerminal.status || "—"}
              </span>
            </div>
            {lrTerminal.recommendation ? (
              <div className="text-live leading-snug pt-1">
                recommends: {cfgStr(lrTerminal.recommendation.config)} — clears the full OOS gate;
                promotion still requires your approval.
              </div>
            ) : (
              <div className="text-muted leading-snug pt-1">
                no change recommended — the champion holds.
              </div>
            )}
          </div>
        ) : (
          <div className="text-[14px] text-muted leading-relaxed">
            no re-measurement yet — on request the researcher refreshes the corpus, re-prices
            every call and re-runs the full grid against the gates.
          </div>
        )}
        <div className="mt-auto pt-3">
          {readOnly ? (
            <div className="text-[13px] text-muted/70 text-center py-1.5 border border-edge/50 bg-black/20">
              re-measurement runs on the live book — switch to LIVE to trigger it
            </div>
          ) : (
            <>
              <button
                onClick={remeasure}
                disabled={spinning}
                className={`w-full flex items-center justify-center gap-2 text-[13px] font-bold uppercase tracking-[0.1em] px-2.5 py-1.5 border ${
                  spinning
                    ? "bg-live/[0.04] border-edge text-muted opacity-60 cursor-not-allowed"
                    : "bg-black/30 border-edge text-muted hover:bg-ink hover:text-base hover:border-ink"
                }`}
              >
                {spinning && <Spinner />}
                {running ? "RE-MEASURING…" : busy ? "STARTING…" : "[ RE-MEASURE NOW ]"}
              </button>
              {err && <div className="text-[13px] text-loss mt-1.5">{err}</div>}
              {stale && !running && !err && (
                <div className="text-[13px] text-live mt-1.5">
                  requested, but the server hasn't started it yet (30s) — it will pick it up when
                  the engine is free; feel free to retry.
                </div>
              )}
            </>
          )}
        </div>
      </div>

      {detailId && (
        <ConfigDetail
          configId={detailId}
          meta={meta}
          champion={champion}
          readOnly={readOnly}
          onClose={() => setDetailId(null)}
          onClone={(params) => {
            setDetailId(null);
            setBuilder({ prefill: params });
          }}
        />
      )}
      {builder && <StrategyBuilder prefill={builder.prefill} onClose={() => setBuilder(null)} />}
    </div>
  );
}
