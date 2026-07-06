import React, { useEffect } from "react";
import TokenTerminal from "./TokenTerminal";

// Shell: near-fullscreen overlay; all the substance lives in TokenTerminal.
export default function TokenModal({ mint, onClose }) {
  useEffect(() => {
    const onKey = (e) => {
      if (e.key === "Escape") onClose?.();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  if (!mint) return null;
  return (
    <div
      className="fixed inset-0 z-50 modal-scrim flex items-center justify-center p-4"
      onClick={onClose}
    >
      <div
        className="panel w-[1200px] max-w-[95vw] h-[85vh] rounded-2xl shadow-2xl overflow-hidden flex flex-col"
        onClick={(e) => e.stopPropagation()}
      >
        <TokenTerminal mint={mint} onClose={onClose} />
      </div>
    </div>
  );
}
