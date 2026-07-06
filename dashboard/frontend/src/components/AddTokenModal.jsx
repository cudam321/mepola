import React, { useState } from "react";
import { manual } from "../api";

// [EXTERNAL INPUT] — paste a contract address, it auto-fetches ticker + FDV + liquidity, then:
//  · ADD WATCHLIST — inject it as a CALL the algo trades (config #1, waits for the −50% dip)
//  · DIRECT BUY    — buy it now at market, or rest a limit order (the algo then rides the fill)

const fmtUsd = (v) =>
  v == null ? "—" : v >= 1e6 ? "$" + (v / 1e6).toFixed(2) + "M"
    : v >= 1e3 ? "$" + (v / 1e3).toFixed(1) + "K" : "$" + Number(v).toPrecision(3);

export default function AddTokenModal({ onClose }) {
  const [ca, setCa] = useState("");
  const [info, setInfo] = useState(null);      // { ticker, price, fdv, liquidity }
  const [looking, setLooking] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  const [ok, setOk] = useState(null);
  const [usd, setUsd] = useState("3");
  const [limit, setLimit] = useState("");

  const valid = ca.trim().length >= 32 && ca.trim().length <= 48;

  const lookup = async () => {
    if (!valid) return;
    setLooking(true);
    setErr(null);
    setInfo(null);
    try {
      setInfo(await manual.lookup(ca.trim()));
    } catch (e) {
      setErr(String(e.message || e));
    } finally {
      setLooking(false);
    }
  };

  const act = async (fn, okMsg) => {
    setBusy(true);
    setErr(null);
    setOk(null);
    try {
      await fn();
      setOk(okMsg);
      setTimeout(onClose, 900);
    } catch (e) {
      setErr(String(e.message || e));
    } finally {
      setBusy(false);
    }
  };

  const mint = ca.trim();
  const ticker = info?.ticker || undefined;
  const addWatch = () => act(() => manual.injectSignal(mint, ticker), "added — the algo is watching it");
  const buyMarket = () =>
    act(() => manual.placeOrder({ mint, ticker, side: "buy", kind: "market",
      size_kind: "usd", size_value: Number(usd) }), "buying at market — algo will ride it");
  const buyLimit = () =>
    act(() => manual.placeOrder({ mint, ticker, side: "buy", kind: "limit",
      trigger_type: "price_at_or_below", trigger_value: Number(limit),
      size_kind: "usd", size_value: Number(usd) }), "limit order resting");

  return (
    <div className="fixed inset-0 z-50 modal-scrim flex items-center justify-center p-6" onClick={onClose}>
      <div className="panel w-[520px] max-w-full p-5 rounded-2xl shadow-2xl" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center justify-between mb-3">
          <span className="panel-title text-[17px] font-bold text-ink">external input</span>
          <button className="text-muted hover:text-ink text-xl leading-none px-1" onClick={onClose}>×</button>
        </div>

        {/* paste CA */}
        <div className="flex gap-1.5">
          <input
            value={ca}
            onChange={(e) => { setCa(e.target.value); setInfo(null); setOk(null); }}
            onKeyDown={(e) => e.key === "Enter" && lookup()}
            placeholder="paste contract address (mint)"
            className="num text-[13px] text-ink bg-black/30 border border-edge focus:border-tail/50 rounded px-2 py-1.5 outline-none flex-1"
          />
          <button
            onClick={lookup}
            disabled={!valid || looking}
            className={`text-[13px] font-bold uppercase px-2 border ${
              valid ? "border-tail/50 text-tail hover:bg-tail hover:text-base" : "border-edge text-muted/40"
            }`}
          >
            {looking ? "…" : "fetch"}
          </button>
        </div>

        {/* stats preview */}
        {info && (
          <div className="mt-3 grid grid-cols-4 gap-2 num text-[13px]">
            <div><div className="tile-label">ticker</div><div className="text-ink font-semibold">{info.ticker || "?"}</div></div>
            <div><div className="tile-label">price</div><div className="text-ink">{info.price != null ? Number(info.price).toPrecision(3) : "—"}</div></div>
            <div><div className="tile-label">fdv</div><div className="text-ink">{fmtUsd(info.fdv)}</div></div>
            <div><div className="tile-label">liquidity</div><div className="text-ink">{fmtUsd(info.liquidity)}</div></div>
          </div>
        )}

        {info && (
          <>
            {/* ADD WATCHLIST */}
            <div className="mt-4 pt-3 border-t border-edge/60">
              <div className="flex items-center gap-2">
                <button
                  onClick={addWatch}
                  disabled={busy}
                  className="text-[13px] font-bold uppercase tracking-[0.06em] px-2.5 py-1.5 border border-live/50 text-live hover:bg-live hover:text-base bg-black/30"
                >
                  add watchlist
                </button>
                <span className="text-[12px] text-muted leading-snug">
                  treat it like a channel call — the algo watches for the −50% dip and runs config #1.
                </span>
              </div>
            </div>

            {/* DIRECT BUY */}
            <div className="mt-3 pt-3 border-t border-edge/60">
              <div className="tile-label mb-1.5">direct buy — algo rides the fill</div>
              <div className="flex items-center gap-1.5 flex-wrap">
                <span className="text-[12px] text-muted">$</span>
                <input value={usd} onChange={(e) => setUsd(e.target.value)} type="number"
                  className="num text-[13px] text-ink bg-black/30 border border-edge rounded px-1.5 py-1 w-16 text-right outline-none" />
                <button onClick={buyMarket} disabled={busy || !(Number(usd) > 0)}
                  className="text-[13px] font-bold uppercase px-2.5 py-1.5 border border-win/60 text-win hover:bg-win hover:text-base bg-black/30">
                  market buy
                </button>
                <span className="text-[12px] text-muted ml-1">or limit @</span>
                <input value={limit} onChange={(e) => setLimit(e.target.value)} type="number" placeholder="price"
                  className="num text-[13px] text-ink bg-black/30 border border-edge rounded px-1.5 py-1 w-24 text-right outline-none" />
                <button onClick={buyLimit} disabled={busy || !(Number(limit) > 0) || !(Number(usd) > 0)}
                  className="text-[13px] font-bold uppercase px-2 py-1.5 border border-edge text-muted hover:bg-ink hover:text-base bg-black/30">
                  set limit
                </button>
              </div>
            </div>
          </>
        )}

        {err && <div className="text-[13px] text-loss mt-2.5">{err}</div>}
        {ok && <div className="text-[13px] text-win mt-2.5">✓ {ok}</div>}
        <div className="text-[12px] text-muted/70 mt-3 leading-snug border-t border-edge/40 pt-2.5">
          buys need the wallet armed (they simulate in paper) · kill-switch blocks new buys ·
          every position is algo-managed until you override it from its token view
        </div>
      </div>
    </div>
  );
}
