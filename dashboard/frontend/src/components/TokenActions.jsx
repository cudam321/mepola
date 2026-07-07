import React, { useEffect, useState } from "react";
import { manual } from "../api";

// Override this ONE token from its own view. Everything is algo-managed by default; the first manual
// action here takes the position over (the algo stops driving it). A WATCHING token can be bought now
// (direct buy); an active hold can be sold, closed, or given a manual TP / SL / trailing stop.

const ACTIVE = ["ENTERED", "SECURED", "RIDING"];

function Btn({ children, onClick, tone = "muted", disabled, title }) {
  const tones = {
    muted: "border-edge text-muted hover:bg-ink hover:text-base hover:border-ink",
    win: "border-win/60 text-win hover:bg-win hover:text-base",
    loss: "border-loss/60 text-loss hover:bg-loss hover:text-base",
    live: "border-live/50 text-live hover:bg-live hover:text-base",
    tail: "border-tail/50 text-tail hover:bg-tail hover:text-base",
  };
  return (
    <button onClick={onClick} disabled={disabled} title={title}
      className={`text-[12px] font-bold uppercase tracking-[0.06em] px-2 py-1 border bg-black/30 transition-colors ${tones[tone]} ${disabled ? "opacity-40 cursor-not-allowed" : ""}`}>
      {children}
    </button>
  );
}

function Field(props) {
  return (
    <input {...props}
      className={`num text-[12px] text-ink bg-black/30 border border-edge focus:border-tail/50 rounded px-1.5 py-1 outline-none ${props.className || ""}`} />
  );
}

export default function TokenActions({ mint, pos, live, onAction }) {
  const [orders, setOrders] = useState([]);
  const [err, setErr] = useState(null);
  const [busy, setBusy] = useState(false);
  const [form, setForm] = useState(null); // 'tp' | 'sl' | 'trail' | null
  const [val, setVal] = useState("");
  const [frac, setFrac] = useState("100");
  const [buyUsd, setBuyUsd] = useState("3");

  const state = pos?.state;
  const isActive = ACTIVE.includes(state);
  const isWatching = state === "WATCHING";
  const isManual = pos?.controller === "manual";
  // audit #25(d): a stop priced at/above the current price triggers price_at_or_below immediately —
  // an accidental full-bag market sell. Block it in the form (and #25(e): frac>0 is enforced below).
  const stopFiresNow =
    form === "sl" && val !== "" && Number(live?.price) > 0 && Number(val) >= Number(live.price);

  const loadOrders = () => manual.ordersFor(mint).then(setOrders).catch(() => setOrders([]));
  useEffect(() => {
    loadOrders();
  }, [mint, pos?.state, pos?.controller]);

  const run = async (fn) => {
    setBusy(true);
    setErr(null);
    try {
      await fn();
      await loadOrders();
      onAction?.();
    } catch (e) {
      setErr(String(e.message || e));
      setTimeout(() => setErr(null), 6000);
    } finally {
      setBusy(false);
    }
  };

  const sell = (f) => run(() => manual.placeOrder({
    mint, ticker: pos?.ticker, side: "sell", kind: "market",
    size_kind: "token_frac", size_value: f }));
  const buyNow = () => run(() => manual.placeOrder({
    mint, ticker: pos?.ticker, side: "buy", kind: "market", size_kind: "usd", size_value: Number(buyUsd) }));
  const release = () => run(async () => {
    try {
      await manual.release(mint);
    } catch (e) {
      // M13: below the −30% line the algo's first act is a full market stop-out — confirm first
      if (String(e.message || "").includes("would stop out immediately")
          && window.confirm("Price is at/below the −30% stop line: the algo will market-sell "
            + "the whole bag the moment it takes over.\n\nRelease anyway?")) {
        await manual.release(mint, { force: true });
        return;
      }
      throw e;
    }
  });
  const cancel = (id) => run(() => manual.cancelOrder(id));
  const submitOrder = () => {
    const map = {
      tp: { kind: "take_profit", trigger_type: "mult_at_or_above" },
      sl: { kind: "stop_loss", trigger_type: "price_at_or_below" },
      trail: { kind: "trailing_stop", trigger_type: "peak_drawdown_pct" },
    }[form];
    return run(async () => {
      await manual.placeOrder({
        mint, ticker: pos?.ticker, side: "sell", ...map,
        trigger_value: form === "trail" ? Number(val) / 100 : Number(val),
        size_kind: "token_frac", size_value: Math.min(1, Math.max(0.01, Number(frac) / 100)),
      });
      setForm(null); setVal("");
    });
  };

  if (!pos || (!isActive && !isWatching)) return null;   // closed / no position → nothing to do

  const trig = (o) =>
    o.trigger_type === "mult_at_or_above" ? `≥ ${o.trigger_value}×`
    : o.trigger_type === "peak_drawdown_pct" ? `trail ${Math.round(o.trigger_value * 100)}%`
    : o.trigger_type === "price_at_or_below" ? `≤ ${Number(o.trigger_value).toPrecision(3)}`
    : o.trigger_type === "price_at_or_above" ? `≥ ${Number(o.trigger_value).toPrecision(3)}`
    : "now";

  return (
    <div className="px-5 py-2 border-t border-edge/60 shrink-0">
      <div className="flex items-center gap-2 flex-wrap">
        <span className="tile-label shrink-0">
          {isManual ? "you're driving this" : isWatching ? "override" : "override the algo"}
        </span>

        {isWatching && (
          <>
            <span className="text-[12px] text-muted">buy now $</span>
            <Field value={buyUsd} onChange={(e) => setBuyUsd(e.target.value)} type="number" className="w-14 text-right" />
            <Btn tone="win" onClick={buyNow} disabled={busy || !(Number(buyUsd) > 0)}
                 title="skip the −50% dip wait and buy now — the algo then rides it">buy now</Btn>
          </>
        )}

        {isActive && (
          <>
            <Btn tone="loss" onClick={() => sell(0.25)} disabled={busy}>sell 25%</Btn>
            <Btn tone="loss" onClick={() => sell(0.5)} disabled={busy}>sell 50%</Btn>
            <Btn tone="loss" onClick={() => sell(1)} disabled={busy}>close</Btn>
            <span className="w-px h-4 bg-edge/70 mx-0.5" />
            <Btn tone="tail" onClick={() => setForm(form === "tp" ? null : "tp")}>+ take-profit</Btn>
            <Btn tone="tail" onClick={() => setForm(form === "sl" ? null : "sl")}>+ stop-loss</Btn>
            <Btn tone="tail" onClick={() => setForm(form === "trail" ? null : "trail")}>+ trailing</Btn>
            {isManual && (
              <Btn onClick={release} disabled={busy} title="hand this position back to the algo (config #1)">→ algo</Btn>
            )}
          </>
        )}
      </div>

      {form && (
        <div className="flex items-center gap-1.5 mt-1.5">
          <span className="text-[12px] text-muted w-16">
            {form === "tp" ? "at ×mult" : form === "sl" ? "at price" : "drop %"}
          </span>
          <Field value={val} onChange={(e) => setVal(e.target.value)} type="number" className="w-24 text-right"
                 placeholder={form === "tp" ? "3" : form === "sl" ? "0.0007" : "25"} />
          <span className="text-[12px] text-muted">sell %</span>
          <Field value={frac} onChange={(e) => setFrac(e.target.value)} type="number" className="w-14 text-right" />
          <Btn tone="win" onClick={submitOrder}
               disabled={busy || !val || !(Number(frac) > 0) || stopFiresNow}
               title={stopFiresNow ? "a stop at/above the current price would sell the whole bag NOW" : undefined}>set</Btn>
        </div>
      )}
      {stopFiresNow && (
        <div className="text-[12px] text-loss mt-1">stop ≥ current price ({Number(live.price).toPrecision(3)}) would fire immediately</div>
      )}

      {orders.length > 0 && (
        <div className="mt-1.5 flex flex-wrap gap-1.5">
          {orders.map((o) => (
            <span key={o.id}
              className="inline-flex items-center gap-1.5 num text-[12px] text-muted border border-edge/70 rounded px-1.5 py-0.5 bg-black/20">
              <span className={o.side === "buy" ? "text-live" : "text-loss"}>{o.kind.replace("_", " ")}</span>
              <span className="text-tail">{trig(o)}</span>
              <span className="text-muted/70">{o.size_kind === "token_frac" ? Math.round(o.size_value * 100) + "%" : o.size_value}</span>
              {o.status === "open" && (
                <button onClick={() => cancel(o.id)} disabled={busy} className="text-loss hover:text-loss/70 font-bold">✕</button>
              )}
            </span>
          ))}
        </div>
      )}
      {err && <div className="text-[12px] text-loss mt-1">{err}</div>}
    </div>
  );
}
