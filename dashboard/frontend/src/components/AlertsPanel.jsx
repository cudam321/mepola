import React from "react";
import InfoHint from "./InfoHint";

const SEV_DOT = {
  CRIT: "bg-loss",
  WARN: "bg-live",
  INFO: "bg-muted",
};
const SEV_TEXT = {
  CRIT: "text-loss",
  WARN: "text-live",
  INFO: "text-muted",
};

const ago = (iso) => {
  if (!iso) return null;
  const s = (Date.now() - Date.parse(iso)) / 1000;
  if (!Number.isFinite(s) || s < 0) return null;
  if (s < 90) return Math.round(s) + "s";
  if (s < 5400) return Math.round(s / 60) + "m";
  if (s < 48 * 3600) return (s / 3600).toFixed(1) + "h";
  return (s / 86400).toFixed(1) + "d";
};

export default function AlertsPanel({ alerts, meta, signals, caps }) {
  const list = alerts || [];
  // MEASURED freshness (never assert liveness we can't see): snapshot age from
  // meta.generated_at, channel recency from the newest signal.
  const snapAge = ago(meta?.generated_at);
  const lastCall = ago(signals?.[0]?.ts);
  const snapStale = meta?.generated_at && Date.now() - Date.parse(meta.generated_at) > 30000;
  return (
    <div className="panel p-3 h-full flex flex-col">
      <div className="flex items-center gap-1.5 mb-2">
        <div className="tile-label">system health</div>
        <InfoHint text="Is the machine alive and behaving — mode, kill-switch, feed freshness, risk limits, and anything that went wrong." />
      </div>
      <div className="text-xs space-y-1.5 mb-3 num">
        <div className="flex justify-between">
          <span className="text-muted">mode</span>
          <span className="text-ink">{meta?.mode}</span>
        </div>
        <div className="flex justify-between">
          <span className="text-muted">kill-switch</span>
          <span className={meta?.kill_switch === "on" ? "text-loss" : "text-win"}>
            {meta?.kill_switch}
          </span>
        </div>
        <div className="flex justify-between">
          <span className="text-muted">source</span>
          <span className="text-ink">{meta?.seed_source || "live"}</span>
        </div>
        <div className="flex justify-between">
          <span className="text-muted">freshness</span>
          <span className={snapStale ? "text-live" : "text-muted"}>
            {snapAge ? `snapshot ${snapAge} old` : "snapshot —"}
            {lastCall ? ` · last call ${lastCall} ago` : ""}
          </span>
        </div>
        {caps && (
          <div className="flex justify-between">
            <span className="text-muted">limits</span>
            <span className="text-muted">
              ${caps.stake}/trade · {caps.maxOpen} open · ${caps.deployedCap} deployed · $
              {caps.dailyLossCap}/day loss
            </span>
          </div>
        )}
      </div>
      <div className="tile-label mb-1.5">alerts</div>
      {/* min-h-0 is REQUIRED: without it a flex child ignores overflow-auto and grows to fit its
          content, spilling the alerts over the positions row below (worse now with manual alerts). */}
      <div className="overflow-auto flex-1 min-h-0 space-y-1.5">
        {list.length === 0 ? (
          <div className="flex items-start gap-2 text-xs text-muted py-0.5">
            <span className="text-win/70 shrink-0 select-none" aria-hidden="true">
              └─
            </span>
            NO ALERTS — bleeding as designed.
          </div>
        ) : (
          list.map((a) => (
            <div key={a.id} className="flex items-baseline gap-2 text-[14px] leading-tight">
              <span
                className={`w-1.5 h-1.5 rounded-full shrink-0 self-center ${
                  SEV_DOT[a.severity] || "bg-muted"
                }`}
              />
              {a.ts && (
                <span className="num text-muted/70 shrink-0">
                  {String(a.ts).slice(11, 19)}
                </span>
              )}
              <span className={`font-semibold ${SEV_TEXT[a.severity] || "text-muted"}`}>
                {a.kind}
              </span>
              <span className="text-muted">{a.message}</span>
            </div>
          ))
        )}
      </div>
    </div>
  );
}
