import React, { useEffect, useState } from "react";
import { currentBook, withBook } from "../api";

// Runtime controls: the ONLY editable knobs are sizing/risk + the kill switch.
// Strategy parameters are research-locked server-side — shown read-only here on purpose.

const FIELDS = [
  { key: "ctl_stake_usd", label: "stake $/trade" },
  { key: "ctl_max_concurrent", label: "max concurrent" },
  { key: "ctl_total_deployed_cap_usd", label: "total deployed cap $" },
  { key: "ctl_daily_loss_cap_usd", label: "daily loss cap $" },
];

// override caps — direct-buy budget (0 = disable direct buys) + per-buy fat-finger clamp
const MANUAL_FIELDS = [
  { key: "manual_cap_usd", label: "direct-buy on/off $ (0 = off)" },
  { key: "manual_trade_hard_cap_usd", label: "per-buy cap $" },
];

async function postControl(key, value) {
  const r = await fetch("/api/control", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ key, value, book: currentBook() }),
  });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || `save failed (${r.status})`);
  return j;
}

function LockIcon() {
  return (
    <svg
      width="10"
      height="10"
      viewBox="0 0 24 24"
      fill="none"
      stroke="#6F8E38"
      strokeWidth="2.4"
      strokeLinecap="round"
      className="shrink-0 opacity-80"
    >
      <rect x="4" y="11" width="16" height="10" rx="2" />
      <path d="M8 11V7a4 4 0 0 1 8 0v4" />
    </svg>
  );
}

function LockedRow({ label, value }) {
  return (
    <div className="flex items-center gap-2.5 text-[14px] leading-tight py-[1px]">
      <LockIcon />
      <span className="text-muted w-20 shrink-0">{label}</span>
      <span className="text-ink/85">{value}</span>
    </div>
  );
}

function NumField({ fieldKey, label, spec }) {
  const [val, setVal] = useState(String(spec.value));
  const [err, setErr] = useState(null);
  const [flash, setFlash] = useState(false);
  const [saving, setSaving] = useState(false);

  const save = async () => {
    setErr(null);
    const n = Number(val);
    if (val.trim() === "" || Number.isNaN(n)) {
      setErr("must be a number");
      return;
    }
    setSaving(true);
    try {
      await postControl(fieldKey, n);
      setFlash(true);
      setTimeout(() => setFlash(false), 1100);
    } catch (e) {
      setErr(e.message);
    } finally {
      setSaving(false);
    }
  };

  const fragile = fieldKey === "ctl_stake_usd" && Number(val) > 5;

  return (
    <div>
      <div className="flex items-center gap-2">
        <span className="text-[14px] text-muted flex-1 min-w-0">{label}</span>
        <input
          type="number"
          min={spec.min}
          max={spec.max}
          step="any"
          value={val}
          onChange={(e) => {
            setVal(e.target.value);
            setErr(null);
          }}
          onKeyDown={(e) => e.key === "Enter" && save()}
          className={`num w-24 text-right text-[15px] text-live bg-live/[0.05] border rounded-lg px-2 py-1 outline-none transition-colors ${
            flash
              ? "border-win/60"
              : err
              ? "border-loss/50"
              : "border-edge focus:border-tail/40"
          }`}
        />
        <button
          onClick={save}
          disabled={saving}
          className={`text-[13px] font-bold uppercase px-1.5 py-1 border w-[58px] shrink-0 ${
            flash
              ? "bg-win text-base border-win"
              : "bg-black/30 border-edge text-muted hover:bg-ink hover:text-base hover:border-ink"
          } ${saving ? "opacity-50" : ""}`}
        >
          {flash ? "SAVED" : "[SAVE]"}
        </button>
      </div>
      <div className="num text-[13px] text-muted/60 mt-1">
        default: {spec.default} · min {spec.min} · max {spec.max}
      </div>
      {fragile && (
        <div className="text-[13px] text-live mt-1">
          size-fragile: $25-fixed measured to bust to $0; hard cap $10
        </div>
      )}
      {err && <div className="text-[13px] text-loss mt-1">{err}</div>}
    </div>
  );
}

export default function ControlsModal({ onClose }) {
  const [ctl, setCtl] = useState(null);
  const [loadErr, setLoadErr] = useState(null);
  const [killErr, setKillErr] = useState(null);

  useEffect(() => {
    fetch(withBook("/api/control"))
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error("bad status"))))
      .then(setCtl)
      .catch(() => setLoadErr("failed to load /api/control"));
  }, []);

  const killOn = ctl?.editable?.kill_switch?.value === "on";

  const toggleKill = async () => {
    const prev = killOn ? "on" : "off";
    const next = killOn ? "off" : "on";
    setKillErr(null);
    // optimistic flip, revert on error
    setCtl((c) => ({ ...c, editable: { ...c.editable, kill_switch: { value: next } } }));
    try {
      await postControl("kill_switch", next);
    } catch (e) {
      setCtl((c) => ({ ...c, editable: { ...c.editable, kill_switch: { value: prev } } }));
      setKillErr(e.message);
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 modal-scrim flex items-center justify-center p-6"
      onClick={onClose}
    >
      <div
        className="panel w-[520px] max-w-full max-h-[86vh] overflow-auto p-5 rounded-2xl shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between mb-4">
          <div className="flex items-baseline gap-2.5 min-w-0">
            <span className="panel-title text-[18px] font-bold tracking-tight text-ink whitespace-nowrap">runtime controls</span>
            <span className="text-[13px] text-muted">sizing only — strategy is locked</span>
          </div>
          <button
            className="text-muted hover:text-ink text-xl leading-none px-1"
            onClick={onClose}
          >
            ×
          </button>
        </div>

        {loadErr ? (
          <div className="text-loss text-sm py-10 text-center">{loadErr}</div>
        ) : !ctl ? (
          <div className="text-muted text-sm py-10 text-center">
            <span className="ascii-spinner mr-2 text-live" aria-hidden="true" />
            LOADING…
          </div>
        ) : (
          <>
            {/* Kill switch — system interlock */}
            <div
              className={`border px-3.5 py-3 flex items-center gap-3 transition-colors ${
                killOn ? "bg-loss/10 border-loss/60" : "bg-black/30 border-edge"
              }`}
            >
              <div className="min-w-0 flex-1">
                <div
                  className={`text-[14px] font-bold tracking-[0.14em] ${
                    killOn ? "text-loss" : "text-ink"
                  }`}
                >
                  KILL SWITCH{killOn ? " — ENGAGED" : ""}
                </div>
                <div className={`text-[13px] mt-0.5 ${killOn ? "text-loss/90" : "text-muted"}`}>
                  {killOn ? "halting all new entries" : "entries enabled — flip to halt all new entries"}
                </div>
              </div>
              <button
                role="switch"
                aria-checked={killOn}
                aria-label="kill switch"
                onClick={toggleKill}
                className={`px-3 py-1.5 border text-[14px] font-bold tracking-[0.14em] shrink-0 transition-colors ${
                  killOn
                    ? "bg-loss text-base border-loss"
                    : "bg-black/30 text-muted border-edge hover:text-loss hover:border-loss/60"
                }`}
              >
                {killOn ? "ENGAGED" : "[ ARM ]"}
              </button>
            </div>
            {killErr && <div className="text-[13px] text-loss mt-1.5">{killErr}</div>}

            {/* Sizing & risk */}
            <div className="mt-4 pt-4 border-t border-edge/60">
              <div className="tile-label mb-2.5">sizing &amp; risk</div>
              <div className="space-y-3">
                {FIELDS.map((f) => (
                  <NumField key={f.key} fieldKey={f.key} label={f.label} spec={ctl.editable[f.key]} />
                ))}
              </div>
            </div>

            {/* Override / direct-buy caps */}
            <div className="mt-4 pt-4 border-t border-edge/60">
              <div className="tile-label mb-2.5">override caps</div>
              <div className="space-y-3">
                {MANUAL_FIELDS.map((f) =>
                  ctl.editable[f.key] ? (
                    <NumField key={f.key} fieldKey={f.key} label={f.label} spec={ctl.editable[f.key]} />
                  ) : null
                )}
              </div>
              <div className="text-[13px] text-muted/70 mt-2 leading-snug">
                direct buys join the algo book (config #1 rides them). Set the first to 0 to disable
                direct buys entirely; the per-buy cap stops a fat-finger. Buys still need the wallet armed.
              </div>
            </div>

            {/* Strategy (locked) */}
            <div className="mt-4 pt-4 border-t border-edge/60">
              <div className="tile-label mb-2.5">strategy (locked)</div>
              <div className="space-y-1">
                <LockedRow label="entry" value="buy the −50% dip (≤48h)" />
                <LockedRow label="stop" value="−30% until secured" />
                <LockedRow label="secure" value="3×: sell 33%, remove stop" />
                <LockedRow label="ride" value="6/12/24/48× (×2) then ×3" />
                <LockedRow label="re-entry" value="never" />
              </div>
              {ctl.locked?.note && (
                <div className="text-[13px] text-muted/70 italic mt-2">{ctl.locked.note}</div>
              )}
              {/* The integrity caveat lives here now (moved off the main chrome by request —
                  the words themselves are a project guardrail; do not soften or remove). */}
              <div className="text-[13px] text-muted mt-2.5 leading-snug border-t border-edge/40 pt-2.5">
                {/* F50: interpolate the LIVE stake — a stale "$3" understates fragility exactly
                    when the user has raised the stake. This makes the caveat truer, never softer. */}
                a deliberate power-law tail bet at ${Number(ctl?.editable?.ctl_stake_usd?.value ?? 3)}/trade ·
                most positions go to zero · the edge is one rare tail · size it only as money you
                can lose entirely
              </div>
            </div>

            {/* Mode */}
            <div className="mt-4 pt-4 border-t border-edge/60">
              <div className="tile-label mb-2">mode</div>
              <div className="flex items-baseline gap-2.5 text-[14px]">
                <span className="num text-ink font-semibold uppercase tracking-wide">
                  {ctl.mode}
                </span>
                <span className="text-muted">{ctl.mode_note}</span>
              </div>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
