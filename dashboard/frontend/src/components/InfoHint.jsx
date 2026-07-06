import React, { useCallback, useRef, useState } from "react";
import { createPortal } from "react-dom";

// A small phosphor [?] marker that reveals an explanation on hover/focus. Used to move
// non-essential descriptive subtitles off the screen so titles + numbers carry it.
//
// The tooltip is rendered through a portal at document.body with position:fixed, so it
// (a) appears INSTANTLY (native `title` has a ~1s delay that reads as broken), and
// (b) never clips inside the overflow-hidden/auto panels a CSS-positioned tooltip would.
const TIP_W = 264;

export default function InfoHint({ text, className = "" }) {
  const ref = useRef(null);
  const [tip, setTip] = useState(null); // { left, top?, bottom?, flip } | null

  const show = useCallback(() => {
    const el = ref.current;
    if (!el) return;
    const r = el.getBoundingClientRect();
    // clamp horizontally so a right-edge hint doesn't overflow the viewport
    let left = r.left;
    if (left + TIP_W > window.innerWidth - 12) left = r.right - TIP_W;
    left = Math.max(12, left);
    // flip above when there isn't room below (bottom-of-page hints)
    const below = r.bottom + 150 <= window.innerHeight;
    setTip(
      below
        ? { left, top: r.bottom + 6 }
        : { left, bottom: window.innerHeight - r.top + 6 }
    );
  }, []);

  const hide = useCallback(() => setTip(null), []);

  if (!text) return null;
  return (
    <>
      <span
        ref={ref}
        tabIndex={0}
        onMouseEnter={show}
        onMouseLeave={hide}
        onFocus={show}
        onBlur={hide}
        aria-label={text}
        className={`num text-[11px] leading-none text-muted/50 cursor-help select-none transition-colors hover:text-tail focus:text-tail outline-none ${className}`}
      >
        [?]
      </span>
      {tip &&
        createPortal(
          <div
            role="tooltip"
            style={{
              position: "fixed",
              left: tip.left,
              top: tip.top,
              bottom: tip.bottom,
              width: TIP_W,
              zIndex: 10000,
              background: "#0a0f06",
              border: "1px solid rgba(147,192,31,0.45)",
              boxShadow: "0 10px 34px rgba(0,0,0,0.72)",
              color: "#A6D63C",
              textShadow: "0 0 1px rgba(147,192,31,0.35)",
            }}
            className="pointer-events-none p-2.5 text-[12px] leading-snug normal-case tracking-normal"
          >
            {text}
          </div>,
          document.body
        )}
    </>
  );
}
