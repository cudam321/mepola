import React from "react";

// Tiny segmented control for LIVE / SEED / ALL data scoping.
export default function ScopeToggle({ value, onChange, options = ["live", "seed", "all"] }) {
  return (
    <div className="flex items-center bg-black/30 border border-edge p-0.5 gap-0.5 shrink-0">
      {options.map((o) => (
        <button
          key={o}
          onClick={() => onChange(o)}
          className={`px-2 py-[3px] text-[12px] font-bold tracking-[0.14em] uppercase ${
            value === o
              ? "bg-tail text-base"
              : "text-muted hover:text-ink hover:bg-live/[0.06]"
          }`}
        >
          {o}
        </button>
      ))}
    </div>
  );
}
