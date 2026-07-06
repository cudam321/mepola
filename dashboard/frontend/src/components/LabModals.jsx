import React, { useEffect, useState } from "react";
import { withBook } from "../api";

// Strategy-lab drill-down + builder. Two overlays:
//  * ConfigDetail — one challenger's knobs, open riders, closed legs (GET /api/lab/{id});
//    custom (X*) strategies can be deleted, any strategy can be cloned into the builder.
//  * StrategyBuilder — add a custom challenger (POST /api/control add_challenger).
//    Forward-only by design: a new strategy starts racing with the NEXT live call.

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

function Scrim({ onClose, children }) {
  return (
    <div
      className="fixed inset-0 z-50 bg-black/80 flex items-center justify-center p-6"
      onClick={onClose}
    >
      <div
        className="panel mepola-panel w-full max-w-[640px] max-h-[84vh] overflow-auto p-4"
        onClick={(e) => e.stopPropagation()}
      >
        {children}
      </div>
    </div>
  );
}

function KnobRow({ label, value }) {
  return (
    <div className="flex justify-between gap-3 text-[14px] py-0.5">
      <span className="text-muted">{label}</span>
      <span className="num text-ink text-right">{value}</span>
    </div>
  );
}

// Human sentences for the knob set — same vocabulary as the header config line.
function knobRows(p) {
  if (!p) return [];
  const rows = [];
  if (p.entry_mode === "delay_1h") rows.push(["entry", "1h after the call, at market +1%"]);
  else if (p.entry_mode === "none" || p.dip === 0) rows.push(["entry", "immediately at the call (chase)"]);
  else rows.push(["entry", `buy the −${Math.round(p.dip * 100)}% dip (≤48h)`]);
  if (p.exit_policy) {
    const ep = p.exit_policy;
    rows.push(["exit", `trail ${Math.round((ep.trail_pct || 0) * 100)}% off peak, arms at ${ep.trail_arm_mult}×`]);
    if (ep.stop_mult) rows.push(["hard stop", `−${Math.round((1 - ep.stop_mult) * 100)}%`]);
    if (ep.time_stop_h && ep.time_stop_h < 1e8) rows.push(["time stop", `${ep.time_stop_h}h`]);
  } else {
    rows.push(["stop", p.sl ? `−${Math.round((1 - p.sl) * 100)}% until secured` : "none"]);
    rows.push(["secure", `${p.ftp}×: sell ${Math.round(p.fsell * 100)}%, remove stop`]);
    rows.push(["ride", "6/12/24/48× (×2) then ×3"]);
  }
  rows.push(["re-entry", p.reentry ? `when price recovers ${p.reentry}× the stop` : "never"]);
  if (p.heat_min != null) rows.push(["gate", `only when market heat ≥ ${p.heat_min}`]);
  return rows;
}

const fmtWhen = (iso) => {
  if (!iso) return "—";
  const d = new Date(iso);
  return (
    d.toLocaleDateString("en-US", { month: "short", day: "numeric" }) +
    " " +
    d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false })
  );
};

export function ConfigDetail({ configId, meta, champion, onClose, onClone, readOnly = false }) {
  const [detail, setDetail] = useState(null);
  const [err, setErr] = useState(null);
  const [confirmDel, setConfirmDel] = useState(false);
  const [deleting, setDeleting] = useState(false);

  useEffect(() => {
    fetch(withBook(`/api/lab/${configId}`))
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error("load failed"))))
      .then(setDetail)
      .catch((e) => setErr(e.message));
  }, [configId]);

  const p = detail?.params;
  const isCustom = configId.startsWith("X");
  const label = p?.label || meta?.[configId]?.label || configId;

  const doDelete = async () => {
    setDeleting(true);
    try {
      await postControl("delete_challenger", configId);
      onClose(true);
    } catch (e) {
      setErr(e.message);
      setDeleting(false);
    }
  };

  return (
    <Scrim onClose={() => onClose(false)}>
      <div className="flex items-center gap-2.5 flex-wrap">
        <span className="num text-[18px] font-bold text-tail">[{configId}]</span>
        <span className="panel-title text-[16px] font-semibold text-ink">{label}</span>
        <span className="mepola-badge px-1.5 text-[12px] text-muted uppercase tracking-[0.1em]">
          {p?.family || meta?.[configId]?.family || "core"}
        </span>
        {configId === champion && (
          <span className="mepola-badge px-1.5 text-tail text-[12px] font-bold tracking-[0.12em]">
            CHAMPION
          </span>
        )}
        <button
          onClick={() => onClose(false)}
          className="ml-auto text-muted hover:text-ink text-[15px] px-1"
          aria-label="close"
        >
          [x]
        </button>
      </div>

      {err && <div className="text-[13px] text-loss mt-2">{err}</div>}

      {p && (
        <div className="mt-3 border border-edge/60 bg-black/20 p-2.5">
          <div className="tile-label mb-1.5">rules</div>
          {knobRows(p).map(([l, v]) => (
            <KnobRow key={l} label={l} value={v} />
          ))}
        </div>
      )}

      <div className="mt-3">
        <div className="tile-label mb-1.5">
          open riders ({detail?.riders?.length ?? "…"})
        </div>
        {detail?.riders?.length ? (
          <table className="w-full text-xs num">
            <thead>
              <tr className="text-left text-[12px] uppercase tracking-[0.14em] text-muted">
                <th className="py-1 font-semibold">ticker</th>
                <th className="font-semibold">status</th>
                <th className="text-right font-semibold">legs done</th>
                <th className="text-right font-semibold" title="in-flight leg marked at the latest price">
                  mark
                </th>
              </tr>
            </thead>
            <tbody>
              {detail.riders.map((r) => (
                <tr key={r.mint} className="border-t border-edge/50">
                  <td className="py-1 font-semibold text-ink">{r.ticker}</td>
                  <td className="text-muted">{(r.status || "").replace(/_/g, " ")}</td>
                  <td className="text-right text-muted">{r.n_legs_done}</td>
                  <td
                    className={`text-right font-semibold ${
                      r.mark_multiple == null
                        ? "text-muted"
                        : r.mark_multiple >= 1
                        ? "text-win"
                        : "text-loss"
                    }`}
                  >
                    {r.mark_multiple != null ? r.mark_multiple.toFixed(2) + "x" : "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <div className="text-[13px] text-muted">none right now</div>
        )}
      </div>

      <div className="mt-3">
        <div className="tile-label mb-1.5">
          closed legs ({detail?.trades?.length ?? "…"})
        </div>
        {detail?.trades?.length ? (
          <table className="w-full text-xs num">
            <thead>
              <tr className="text-left text-[12px] uppercase tracking-[0.14em] text-muted">
                <th className="py-1 font-semibold">closed</th>
                <th className="font-semibold">ticker</th>
                <th className="font-semibold">reason</th>
                <th className="text-right font-semibold">multiple</th>
                <th className="text-right font-semibold">p&amp;l @$3</th>
              </tr>
            </thead>
            <tbody>
              {detail.trades.map((t, i) => (
                <tr key={i} className="border-t border-edge/50">
                  <td className="py-1 text-muted whitespace-nowrap">{fmtWhen(t.closed_at)}</td>
                  <td className="font-semibold text-ink">{t.ticker || t.mint?.slice(0, 4)}</td>
                  <td className="text-muted">{(t.close_reason || "").replace(/_/g, " ")}</td>
                  <td
                    className={`text-right font-semibold ${
                      (t.realized_multiple || 0) >= 1 ? "text-win" : "text-loss"
                    }`}
                  >
                    {t.realized_multiple != null ? t.realized_multiple.toFixed(2) + "x" : "—"}
                  </td>
                  <td
                    className={`text-right ${
                      (t.realized_multiple || 0) >= 1 ? "text-win" : "text-loss"
                    }`}
                  >
                    {t.realized_multiple != null ? signed(3 * (t.realized_multiple - 1)) : "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <div className="text-[13px] text-muted">no closed legs yet</div>
        )}
      </div>

      <div className="mt-4 pt-3 border-t border-edge/60 flex items-center gap-2 flex-wrap">
        {readOnly ? (
          // paper view: the challenger set is the LIVE book's — mutations are hidden here (a paper
          // "delete" would purge the LIVE X-config's race history; ids overlap across books).
          <span className="text-[12px] text-muted/70">
            viewing on the paper book — clone / delete act on the live challenger set (switch to LIVE)
          </span>
        ) : p?.exit_policy || (p?.ftp != null && p.ftp >= 1e8) ? (
          // Configs outside the builder's 5-knob space: trailing-exit (exit_policy) OR
          // never-secure (ftp>=1e8, e.g. C10 diamond hand — the builder's secure-at-x min is
          // 1.1 and would collapse ftp=1e9 to 3.0, silently inverting the strategy). F48.
          <span
            className="text-[13px] font-bold uppercase tracking-[0.1em] px-2.5 py-1.5 border bg-black/20 border-edge/60 text-muted/40 cursor-not-allowed"
            title="trailing-exit and never-secure strategies can't be expressed in the builder's knobs"
          >
            [ CLONE AS NEW STRATEGY ]
          </span>
        ) : (
          <button
            onClick={() => onClone(p)}
            className="text-[13px] font-bold uppercase tracking-[0.1em] px-2.5 py-1.5 border bg-black/30 border-edge text-muted hover:bg-ink hover:text-base hover:border-ink"
          >
            [ CLONE AS NEW STRATEGY ]
          </button>
        )}
        {!readOnly && isCustom ? (
          confirmDel ? (
            <span className="flex items-center gap-2 text-[13px]">
              <span className="text-loss">delete {configId} and its race history?</span>
              <button
                onClick={doDelete}
                disabled={deleting}
                className="font-bold uppercase tracking-[0.1em] px-2 py-1 border border-loss/60 text-loss hover:bg-loss hover:text-base"
              >
                {deleting ? "DELETING…" : "[ YES, DELETE ]"}
              </button>
              <button onClick={() => setConfirmDel(false)} className="text-muted hover:text-ink">
                cancel
              </button>
            </span>
          ) : (
            <button
              onClick={() => setConfirmDel(true)}
              className="text-[13px] font-bold uppercase tracking-[0.1em] px-2.5 py-1.5 border bg-black/30 border-edge text-loss/80 hover:border-loss/60 hover:text-loss"
            >
              [ DELETE ]
            </button>
          )
        ) : !readOnly ? (
          <span className="text-[12px] text-muted/70 ml-auto">
            built-in — part of the fixed, versioned set
          </span>
        ) : null}
      </div>
    </Scrim>
  );
}

// -- builder ---------------------------------------------------------------------- //

// Module-level so inputs keep focus across re-renders (an inline component would remount).
function Field({ label, hint, children }) {
  return (
    <label className="block">
      <span className="text-[12px] tracking-[0.12em] uppercase text-muted/80 font-semibold">
        {label}
      </span>
      <div className="mt-1">{children}</div>
      {hint && <div className="text-[12px] text-muted/70 mt-0.5">{hint}</div>}
    </label>
  );
}
const inputCls =
  "w-full num text-[14px] px-2 py-1.5 bg-black/30 border border-edge text-ink outline-none focus:border-tail/60";

const BLANK = {
  label: "",
  entry_mode: "dip",
  dip_pct: 50, // % below the call price
  stop_pct: 30, // % below entry; 0 = no stop
  ftp: 3.0,
  fsell_pct: 33,
  reentry: "", // recovery multiple of the stop price; empty = never
};

export function StrategyBuilder({ prefill, onClose }) {
  const [f, setF] = useState(() => {
    if (!prefill) return BLANK;
    return {
      label: "",
      entry_mode: prefill.entry_mode === "delay_1h" ? "delay_1h" : prefill.dip === 0 ? "none" : "dip",
      dip_pct: Math.round((prefill.dip || 0) * 100) || 50,
      stop_pct: prefill.sl ? Math.round((1 - prefill.sl) * 100) : 0,
      ftp: prefill.ftp && prefill.ftp < 1e8 ? prefill.ftp : 3.0,
      fsell_pct: Math.round((prefill.fsell || 0.33) * 100),
      reentry: prefill.reentry || "",
    };
  });
  const [err, setErr] = useState(null);
  const [saving, setSaving] = useState(false);
  const [done, setDone] = useState(null);

  const set = (k) => (e) => setF({ ...f, [k]: e.target.value });

  const submit = async () => {
    setErr(null);
    // client-side checks in the FORM's units (the server speaks sim-internal stop levels).
    // F49: mirror ALL backend bounds so invalid input fails inline, not on the POST.
    if (!f.label.trim() || f.label.trim().length > 24) {
      setErr("name must be 1–24 characters");
      return;
    }
    const sp = Number(f.stop_pct);
    if (sp !== 0 && (sp < 5 || sp > 90)) {
      setErr("stop % must be 0 (no stop) or between 5 and 90");
      return;
    }
    if (f.entry_mode === "dip" && (Number(f.dip_pct) < 5 || Number(f.dip_pct) > 90)) {
      setErr("dip trigger % must be between 5 and 90");
      return;
    }
    if (!(Number(f.ftp) >= 1.01) || Number(f.ftp) > 1000) {
      setErr("secure-at (ftp) must be between 1.01 and 1000x");
      return;
    }
    if (!(Number(f.fsell_pct) > 0) || Number(f.fsell_pct) > 100) {
      setErr("sell % at secure must be between 1 and 100");
      return;
    }
    if (f.reentry !== "" && (Number(f.reentry) < 1.1 || Number(f.reentry) > 20)) {
      setErr("re-entry multiple must be between 1.1 and 20 (or empty for none)");
      return;
    }
    setSaving(true);
    try {
      const j = await postControl("add_challenger", {
        label: f.label.trim(),
        entry_mode: f.entry_mode,
        dip: f.entry_mode === "dip" ? Number(f.dip_pct) / 100 : 0,
        sl: Number(f.stop_pct) > 0 ? 1 - Number(f.stop_pct) / 100 : 0,
        ftp: Number(f.ftp),
        fsell: Number(f.fsell_pct) / 100,
        reentry: f.reentry === "" ? null : Number(f.reentry),
      });
      setDone(j.id);
    } catch (e) {
      setErr(e.message);
    } finally {
      setSaving(false);
    }
  };

  return (
    <Scrim onClose={() => onClose(!!done)}>
      <div className="flex items-center gap-2.5">
        <span className="panel-title text-[16px] font-semibold text-ink">new strategy</span>
        <span className="mepola-badge px-1.5 text-[12px] text-muted uppercase tracking-[0.1em]">
          custom
        </span>
        <button
          onClick={() => onClose(!!done)}
          className="ml-auto text-muted hover:text-ink text-[15px] px-1"
          aria-label="close"
        >
          [x]
        </button>
      </div>

      {done ? (
        <div className="mt-4 space-y-2">
          <div className="text-win text-[15px] font-semibold">
            [{done}] is in the race{f.label ? ` — ${f.label}` : ""}.
          </div>
          <div className="text-[14px] text-muted leading-relaxed">
            forward-only: it starts shadow-trading with the NEXT live call (the engine picks
            it up within ~30s). it never touches the paper account — evidence first.
          </div>
          <button
            onClick={() => onClose(true)}
            className="mt-2 text-[13px] font-bold uppercase tracking-[0.1em] px-2.5 py-1.5 border bg-black/30 border-edge text-muted hover:bg-ink hover:text-base hover:border-ink"
          >
            [ DONE ]
          </button>
        </div>
      ) : (
        <div className="mt-3 space-y-3">
          <Field label="name" hint="what shows in the race table (1-24 chars)">
            <input className={inputCls} value={f.label} onChange={set("label")} maxLength={24}
                   placeholder="e.g. dip -45 / stop -25" />
          </Field>

          <Field label="entry">
            <div className="flex gap-1">
              {[
                ["dip", "BUY THE DIP"],
                ["none", "CHASE AT CALL"],
                ["delay_1h", "WAIT 1H"],
              ].map(([k, l]) => (
                <button
                  key={k}
                  onClick={() => setF({ ...f, entry_mode: k })}
                  className={`px-2 py-1 text-[12px] font-bold tracking-[0.1em] border ${
                    f.entry_mode === k
                      ? "bg-tail text-base border-tail"
                      : "bg-black/30 border-edge text-muted hover:text-ink"
                  }`}
                >
                  {l}
                </button>
              ))}
            </div>
          </Field>

          <div className="grid grid-cols-2 gap-3">
            {f.entry_mode === "dip" && (
              <Field label="dip trigger %" hint="buy when price falls this far below the call (≤48h window)">
                <input className={inputCls} type="number" min="5" max="90" value={f.dip_pct}
                       onChange={set("dip_pct")} />
              </Field>
            )}
            <Field label="stop %" hint="cut if it falls this far below entry · 0 = no stop">
              <input className={inputCls} type="number" min="0" max="90" value={f.stop_pct}
                     onChange={set("stop_pct")} />
            </Field>
            <Field label="secure at ×" hint="first take-profit multiple (removes the stop)">
              <input className={inputCls} type="number" step="0.1" min="1.1" value={f.ftp}
                     onChange={set("ftp")} />
            </Field>
            <Field label="sell % at secure" hint="fraction of the position sold at the secure">
              <input className={inputCls} type="number" min="1" max="100" value={f.fsell_pct}
                     onChange={set("fsell_pct")} />
            </Field>
            <Field label="re-entry ×" hint="after a stop: re-enter when price recovers this multiple of the stop · empty = never">
              <input className={inputCls} type="number" step="0.1" placeholder="never"
                     value={f.reentry} onChange={set("reentry")} />
            </Field>
          </div>

          <div className="text-[13px] text-muted leading-snug border-t border-edge/40 pt-2.5">
            after securing, every strategy rides the same ladder (sell 25% of the bag at
            6/12/24/48× then ×3) — the lab isolates entry/stop/secure/re-entry, the knobs the
            research showed matter. shadow-only: it races with $3 phantom stakes and can never
            touch the account or become champion without your promotion.
          </div>

          {err && <div className="text-[13px] text-loss">{err}</div>}

          <button
            onClick={submit}
            disabled={saving || !f.label.trim()}
            className="w-full text-[13px] font-bold uppercase tracking-[0.1em] px-2.5 py-2 border bg-black/30 border-edge text-muted hover:bg-ink hover:text-base hover:border-ink disabled:opacity-40 disabled:cursor-not-allowed"
          >
            {saving ? "ADDING…" : "[ ADD TO THE RACE ]"}
          </button>
        </div>
      )}
    </Scrim>
  );
}
