import React from "react";

// ────────────────────────────────────────────────────────────────────────────
// THE DIVE GAUGE — per-watcher price corridor.
//
// Two glances per row:
//   1. the JOURNEY  — a 60px sparkline inset (time → , % vs call ↑) built from
//      a client-side trail of every snapshot we've seen for this mint;
//   2. the POSITION — a price corridor (+60% … −60% vs the CALL price on the
//      x-axis) carrying the live dot, the deepest-low tick, the CALL line and
//      the lime −50% BUY GATE the engine is waiting for.
// ────────────────────────────────────────────────────────────────────────────

// module-level trail store: mint -> [{ t, pct }]
const TRAILS = new Map();
const MAX_PTS = 400;

/** Feed on every snapshot: accumulates each watcher's pct_from_call and
 *  drops trails for mints no longer present. */
export function feedTrails(positions) {
  const present = new Set();
  const now = Date.now();
  for (const p of positions || []) {
    present.add(p.mint);
    if (p.pct_from_call == null) continue;
    let arr = TRAILS.get(p.mint);
    if (!arr) {
      arr = [];
      TRAILS.set(p.mint, arr);
    }
    const last = arr[arr.length - 1];
    if (!last || last.pct !== p.pct_from_call || now - last.t > 15000) {
      arr.push({ t: now, pct: p.pct_from_call });
      if (arr.length > MAX_PTS) arr.splice(0, arr.length - MAX_PTS);
    }
  }
  for (const k of TRAILS.keys()) if (!present.has(k)) TRAILS.delete(k);
}

/** Row-level proximity treatment: lime glow as price approaches the gate. */
export function watcherRowClass(p) {
  const pct = p?.pct_from_call;
  if (pct == null) return "";
  if (pct <= -47) return "bg-tail/[0.04]";
  return "";
}

const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
// corridor x-axis is PRICE: +60% at the left edge … −60% at the right edge
const xPct = (v) => ((60 - clamp(v, -60, 60)) / 120) * 100;
const fmtCallPct = (v) =>
  v === 0 ? "0%" : (v > 0 ? "+" : "−") + Math.abs(Math.round(v)) + "%";

// ── 48h entry-window clock as a depleting ring ─────────────────────────────
function DeadlineRing({ hLeft }) {
  if (hLeft == null) return <span className="w-[14px] shrink-0" />;
  const frac = clamp(hLeft / 48, 0, 1);
  const R = 5;
  const C = 2 * Math.PI * R;
  const color = hLeft < 4 ? "#FF5147" : hLeft < 12 ? "#93C01F" : "#6F8E38";
  const label =
    hLeft < 1
      ? `${Math.max(0, Math.round(hLeft * 60))}m left in the 48h entry window`
      : `${Math.round(hLeft)}h left in the 48h entry window`;
  return (
    <span className="shrink-0 flex items-center" title={label}>
      <svg width="14" height="14" viewBox="0 0 14 14">
        <circle cx="7" cy="7" r={R} fill="none" stroke="rgba(147,192,31,0.16)" strokeWidth="2" />
        <circle
          cx="7"
          cy="7"
          r={R}
          fill="none"
          stroke={color}
          strokeWidth="2"
          strokeLinecap="butt"
          strokeDasharray={C}
          strokeDashoffset={C * (1 - frac)}
          transform="rotate(-90 7 7)"
          opacity="0.9"
        />
      </svg>
    </span>
  );
}

// ── the journey inset: time →, % vs call ↑ ─────────────────────────────────
function TrailSparkline({ mint, cur }) {
  const trail = TRAILS.get(mint) || [];
  const W = 60;
  const H = 36;
  if (trail.length < 2) {
    return (
      <svg
        width={W}
        height={H}
        className="shrink-0"
        aria-hidden="true"
      >
        <line x1="0" y1={H / 2} x2={W} y2={H / 2} stroke="rgba(147,192,31,0.08)" />
        {cur != null && (
          <circle cx={W - 4} cy={H / 2} r="1.5" fill="#93C01F" opacity="0.7" />
        )}
      </svg>
    );
  }
  let lo = Math.min(...trail.map((d) => d.pct), -50);
  let hi = Math.max(...trail.map((d) => d.pct), 0);
  const pad = Math.max((hi - lo) * 0.12, 2);
  lo -= pad;
  hi += pad;
  const y = (pct) => H - ((pct - lo) / (hi - lo)) * H;
  const t0 = trail[0].t;
  const t1 = trail[trail.length - 1].t;
  const span = Math.max(t1 - t0, 1);
  const x = (t) => ((t - t0) / span) * (W - 5) + 1;
  const pts = trail.map((d) => `${x(d.t).toFixed(1)},${y(d.pct).toFixed(1)}`).join(" ");
  const last = trail[trail.length - 1];
  return (
    <svg
      width={W}
      height={H}
      className="shrink-0"
      aria-hidden="true"
    >
      <title>the journey since we started watching (time →)</title>
      {/* call level + gate level references, if in view */}
      <line x1="0" y1={y(0)} x2={W} y2={y(0)} stroke="rgba(166,214,60,0.14)" strokeDasharray="2 3" />
      <line x1="0" y1={y(-50)} x2={W} y2={y(-50)} stroke="rgba(203,241,78,0.35)" strokeDasharray="2 3" />
      <polyline points={pts} fill="none" stroke="rgba(147,192,31,0.55)" strokeWidth="1" />
      <circle cx={x(last.t)} cy={y(last.pct)} r="1.8" fill="#93C01F" />
    </svg>
  );
}

// ── the corridor: price on x, gate on the right ────────────────────────────
function Corridor({ cur, low, near, hot }) {
  const gateEdge = xPct(-50); // 91.67%
  const H = 36;
  const mid = H / 2;
  return (
    <div
      className="relative flex-1 min-w-[160px] h-[36px]"
      title="price corridor vs the CALL price: +60% (left) → −60% (right) — the lime band on the right is the −50% buy gate"
      style={
        hot
          ? { filter: "drop-shadow(0 0 9px rgba(203,241,78,0.6))" }
          : near
          ? { filter: "drop-shadow(0 0 5px rgba(203,241,78,0.3))" }
          : undefined
      }
    >
      <svg width="100%" height={H} className="absolute inset-0 overflow-visible">
        <defs>
          {/* the dive path: call → gate, brightening toward the buy zone */}
          <linearGradient id="diveGrad" x1="0" y1="0" x2="1" y2="0">
            <stop offset="0" stopColor="rgba(203,241,78,0)" />
            <stop offset="1" stopColor="rgba(203,241,78,0.30)" />
          </linearGradient>
        </defs>
        {/* above-call region: subtle green tint */}
        <rect x="0" y={mid - 8} width="50%" height="16" fill="rgba(61,220,132,0.045)" rx="0" />
        {/* the dive path bar: call → gate */}
        <rect
          x="50%"
          y={mid - 3.5}
          width={`${gateEdge - 50}%`}
          height="7"
          fill="url(#diveGrad)"
          rx="0"
        />
        {/* baseline */}
        <line x1="0" y1={mid} x2="100%" y2={mid} stroke="rgba(147,192,31,0.12)" />
        {/* quiet ±30% ticks */}
        <line x1={`${xPct(30)}%`} y1={mid - 3} x2={`${xPct(30)}%`} y2={mid + 3} stroke="rgba(147,192,31,0.16)" />
        <line x1={`${xPct(-30)}%`} y1={mid - 3} x2={`${xPct(-30)}%`} y2={mid + 3} stroke="rgba(147,192,31,0.16)" />
        {/* the CALL line at 0% */}
        <line x1="50%" y1="4" x2="50%" y2={H - 4} stroke="rgba(166,214,60,0.75)" strokeWidth="1" />
        <text x="50%" y="8" textAnchor="middle" fontSize="9" fill="#6F8E38" letterSpacing="0.08em" fontFamily='DepartureMono, "Pixelify Sans", monospace'>
          CALL
        </text>
        {/* the ENTRY GATE band: −50% … −60% */}
        <rect
          x={`${gateEdge}%`}
          y="3"
          width={`${100 - gateEdge}%`}
          height={H - 6}
          fill="rgba(203,241,78,0.12)"
          rx="0"
        />
        <line
          x1={`${gateEdge}%`}
          y1="3"
          x2={`${gateEdge}%`}
          y2={H - 3}
          stroke="#CBF14E"
          strokeWidth={near || hot ? 1.6 : 1}
          opacity={hot ? 1 : near ? 0.9 : 0.55}
        />
        <text
          x={`${gateEdge + (100 - gateEdge) / 2}%`}
          y="10.5"
          textAnchor="middle"
          fontSize="9"
          fill="#CBF14E"
          letterSpacing="0.06em"
          fontFamily='DepartureMono, "Pixelify Sans", monospace'
          opacity={near || hot ? 1 : 0.8}
        >
          BUY
        </text>
        <text
          x={`${gateEdge + (100 - gateEdge) / 2}%`}
          y="18.5"
          textAnchor="middle"
          fontSize="9"
          fill="#CBF14E"
          letterSpacing="0.02em"
          fontFamily='DepartureMono, "Pixelify Sans", monospace'
          opacity={near || hot ? 1 : 0.8}
        >
          −50%
        </text>
        {/* deepest-low watermark */}
        {low != null && (
          <line
            x1={`${xPct(low)}%`}
            y1={mid - 7}
            x2={`${xPct(low)}%`}
            y2={mid + 7}
            stroke="#FF5147"
            strokeWidth="1.5"
            opacity="0.85"
          >
            <title>{`deepest so far: ${fmtCallPct(low)} from the call price`}</title>
          </line>
        )}
        {/* the live dot */}
        {cur != null && (
          <>
            <circle cx={`${xPct(cur)}%`} cy={mid} r="7" fill="rgba(147,192,31,0.16)" />
            <circle cx={`${xPct(cur)}%`} cy={mid} r="4" fill="#93C01F" className="pulse-dot">
              <title>{`now: ${fmtCallPct(cur)} from the call price`}</title>
            </circle>
          </>
        )}
      </svg>
    </div>
  );
}

// ── the row widget ─────────────────────────────────────────────────────────
export default function DiveGauge({ p }) {
  const cur = p.pct_from_call; // % vs call right now (negative = down)
  const low = p.low_pct_from_call; // deepest % vs call so far
  const near = cur != null && cur <= -40;
  const hot = cur != null && cur <= -47;

  return (
    <div className="flex items-center gap-2.5 h-[44px]">
      <TrailSparkline mint={p.mint} cur={cur} />
      <span className="w-px self-stretch my-2 bg-live/[0.07] shrink-0" />
      <Corridor cur={cur} low={low} near={near} hot={hot} />
      <span className="num text-[13px] whitespace-nowrap text-right min-w-[168px] shrink-0 leading-tight">
        {cur == null ? (
          <span className="text-muted/70">waiting for first price…</span>
        ) : cur > 0 ? (
          <>
            <span className="text-win">+{Math.round(cur)}% above call</span>
            <span className="text-muted"> · not chasing</span>
            {low != null && low < 0 && (
              <span className="block text-muted/60">deepest {fmtCallPct(low)}</span>
            )}
          </>
        ) : (
          <>
            <span className={hot ? "text-tail font-semibold" : "text-live"}>
              {fmtCallPct(cur)} from call
            </span>
            {low != null && (
              <span className="text-loss/80"> · deepest {fmtCallPct(low)}</span>
            )}
            {hot && <span className="block text-tail/80">about to trigger</span>}
          </>
        )}
      </span>
      <DeadlineRing hLeft={p.dip_deadline_h_left} />
    </div>
  );
}
