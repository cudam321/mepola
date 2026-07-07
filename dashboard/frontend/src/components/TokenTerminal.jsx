import React, { useEffect, useRef, useState } from "react";
import * as echarts from "echarts";
import { igniteClass } from "../ignite";
import { currentBook, withBook } from "../api";
import TokenActions from "./TokenActions";

// ────────────────────────────────────────────────────────────────────────────
// THE TOKEN TERMINAL — a real trading-terminal view for one token.
//   header : ticker · mint (copy) · DexScreener/Solscan · LIVE price strip
//   chart  : candlesticks + volume, every strategy level as a markLine,
//            lifecycle events pinned on price, log/linear, range+interval
//   footer : position stat chips + the lifecycle dot-rail
// Data: /api/token/{mint} (position+events+rungs)
//       /api/token/{mint}/candles?range&interval   (poll 30s)
//       /api/token/{mint}/live                     (poll 4s)
// ────────────────────────────────────────────────────────────────────────────

const MONO = 'DepartureMono, "Pixelify Sans", ui-monospace, monospace';
const X_SYMBOL =
  "path://M2,0 L5,3 L8,0 L10,2 L7,5 L10,8 L8,10 L5,7 L2,10 L0,8 L3,5 L0,2 Z";

const EVENT_STYLE = {
  SIGNAL: { symbol: "circle", color: "#6F8E38", size: 7, verb: "channel call" },
  ENTER: { symbol: "triangle", color: "#93C01F", size: 11, verb: "we bought (−50% dip fill)" },
  TP: { symbol: "diamond", color: "#3DDC84", size: 10, verb: "took profit" },
  SECURE: { symbol: "diamond", color: "#3DDC84", size: 10, verb: "secured stake" },
  RIDE_SELL: { symbol: "diamond", color: "#3DDC84", size: 10, verb: "ladder sell" },
  STOP_OUT: { symbol: X_SYMBOL, color: "#FF5147", size: 11, verb: "stopped out" },
  FINALIZE: { symbol: "circle", color: "#6F8E38", size: 6, verb: "position closed" },
  EXPIRE: { symbol: "circle", color: "#6F8E38", size: 6, verb: "watch expired (never dipped)" },
  MANUAL_SELL: { symbol: "diamond", color: "#E8C547", size: 10, verb: "manual sell" },
  MANUAL_BUY: { symbol: "triangle", color: "#E8C547", size: 11, verb: "manual buy" },
  MANUAL_SELL_SUBMITTED: { symbol: "circle", color: "#E8C547", size: 6, verb: "manual sell sent" },
  MANUAL_BUY_SUBMITTED: { symbol: "circle", color: "#E8C547", size: 6, verb: "manual buy sent" },
};

const STATE_TONE = {
  WATCHING: "text-muted",
  ENTERED: "text-live",
  SECURED: "text-tail",
  RIDING: "text-win",
  EXITED: "text-ink",
  EXPIRED: "text-muted",
};

// ── formatting ──────────────────────────────────────────────────────────────
function fmtPrice(p) {
  if (p == null || !isFinite(p)) return "—";
  if (p === 0) return "0";
  if (p >= 1000) return p.toLocaleString("en-US", { maximumFractionDigits: 1 });
  if (p >= 0.001) return p.toPrecision(4);
  return p.toFixed(Math.max(0, -Math.floor(Math.log10(p)) + 3));
}
function fmtUsd(v) {
  if (v == null || !isFinite(v)) return "—";
  const a = Math.abs(v);
  if (a >= 1e9) return "$" + (v / 1e9).toFixed(2) + "B";
  if (a >= 1e6) return "$" + (v / 1e6).toFixed(2) + "M";
  if (a >= 1e3) return "$" + (v / 1e3).toFixed(1) + "K";
  return "$" + v.toFixed(2);
}
function fmtSignedPct(v, digits = 1) {
  if (v == null || !isFinite(v)) return "—";
  const a = Math.abs(v);
  const body =
    a >= 1000
      ? Math.round(a).toLocaleString("en-US")
      : a.toFixed(digits);
  return (v >= 0 ? "+" : "−") + body + "%";
}
const shortMint = (m) => (m ? m.slice(0, 4) + "…" + m.slice(-4) : "");

function fmtAxisTime(iso, interval) {
  const d = new Date(iso);
  if (interval === "1d")
    return d.toLocaleDateString("en-US", { month: "short", day: "numeric" });
  if (interval === "1m")
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
  return (
    d.toLocaleDateString("en-US", { month: "short", day: "numeric" }) +
    " " +
    d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false })
  );
}

// ── chart option ────────────────────────────────────────────────────────────
// Drop garbage candles the data feed sometimes returns (e.g. open=10.00, high<low) — they
// corrupt the tooltip/axes. A valid bar has all-positive prices with low ≤ open,close ≤ high.
function _sane(c) {
  const [, o, h, l, cl] = c;
  return o > 0 && h > 0 && l > 0 && cl > 0 && h >= l && h >= o && h >= cl && l <= o && l <= cl;
}

function buildOption(data, events, yLog) {
  const candles = (data.candles || []).filter(_sane);
  const itv = data.interval;
  const cats = candles.map((c) => c[0]);
  const times = candles.map((c) => Date.parse(c[0]));
  const k = candles.map((c) => [c[1], c[4], c[3], c[2]]); // open, close, low, high
  const vols = candles.map((c) => ({
    value: c[5] ?? 0,
    itemStyle: {
      color: c[4] >= c[1] ? "rgba(61,220,132,0.32)" : "rgba(255,81,71,0.32)",
    },
  }));

  // strategy levels → markLines
  const L = data.levels || {};
  const label = (txt, color, position = "end") => ({
    formatter: txt,
    position,
    color,
    fontSize: 11,
    fontFamily: MONO,
    padding: [0, 0, 0, 4],
  });
  // NOTE: yAxis passed as a STRING — numeric yAxis markLines silently fail to
  // render on ECharts log axes (verified via SSR repro); the string form works
  // on both log and linear.
  const ml = [];
  if (L.call)
    ml.push({
      yAxis: String(L.call),
      lineStyle: { color: "#A6D63C", width: 1, type: "solid", opacity: 0.85 },
      label: label("CALL", "#A6D63C"),
    });
  if (L.entry_gate)
    ml.push({
      yAxis: String(L.entry_gate),
      lineStyle: { color: "#CBF14E", width: 1.6, type: "solid", opacity: 0.9 },
      label: label("BUY ZONE −50%", "#CBF14E"),
    });
  if (L.entry)
    ml.push({
      yAxis: String(L.entry),
      lineStyle: { color: "#93C01F", width: 1, type: "dashed", opacity: 0.9 },
      label: label("ENTRY", "#93C01F", "insideEndTop"),
    });
  if (L.stop)
    ml.push({
      yAxis: String(L.stop),
      lineStyle: { color: "#FF5147", width: 1, type: "dotted" },
      label: label("STOP −30%", "#FF5147"),
    });
  for (const r of L.rungs || [])
    ml.push({
      yAxis: String(r.price),
      lineStyle: { color: "rgba(203,241,78,0.32)", width: 1, type: "dashed" },
      label: label(`${Math.round(r.mult)}×`, "rgba(203,241,78,0.75)"),
    });

  // lifecycle events pinned at (nearest candle, event price);
  // events outside the loaded window are dropped, not clamped
  const evPts = [];
  if (times.length) {
    const stepMs =
      times.length > 1 ? times[1] - times[0] : 60 * 60 * 1000;
    const lo = times[0] - stepMs;
    const hi = times[times.length - 1] + stepMs;
    for (const e of events || []) {
      if (e.price == null) continue;
      const st = EVENT_STYLE[e.event_type] || EVENT_STYLE.FINALIZE;
      const t = Date.parse(e.ts);
      if (!(t >= lo && t <= hi)) continue;
      let idx = times.findIndex((x) => x >= t);
      if (idx === -1) idx = times.length - 1;
      evPts.push({
        value: [Math.max(0, idx), e.price],
        symbol: st.symbol,
        symbolSize: st.size,
        itemStyle: { color: st.color, borderColor: "rgba(6,10,5,0.9)", borderWidth: 1 },
        raw: e,
      });
    }
  }

  return {
    backgroundColor: "transparent",
    animation: false,
    axisPointer: {
      link: [{ xAxisIndex: "all" }],
      lineStyle: { color: "rgba(147,192,31,0.3)" },
      label: { backgroundColor: "#0C1409", fontSize: 12, fontFamily: MONO },
    },
    tooltip: {
      trigger: "axis",
      axisPointer: { type: "cross" },
      backgroundColor: "rgba(6,10,5,0.96)",
      borderColor: "rgba(147,192,31,0.55)",
      textStyle: { color: "#A6D63C", fontSize: 14, fontFamily: MONO },
      extraCssText:
        "border-radius:2px;box-shadow:0 8px 30px rgba(0,0,0,.5);padding:9px 11px;",
      formatter: (ps) => {
        if (!Array.isArray(ps) || !ps.length) return "";
        const idx = ps[0].dataIndex;
        const lines = [
          `<div style="color:#6F8E38;margin-bottom:4px">${fmtAxisTime(cats[idx], itv === "1d" ? "1h" : itv)}</div>`,
        ];
        const c = ps.find((p) => p.seriesType === "candlestick");
        if (c && Array.isArray(c.data)) {
          const [o, cl, lo, hi] = c.data;
          const up = cl >= o;
          const col = up ? "#3DDC84" : "#FF5147";
          lines.push(
            `O ${fmtPrice(o)}&nbsp; H ${fmtPrice(hi)}<br/>L ${fmtPrice(lo)}&nbsp; C <span style="color:${col}">${fmtPrice(cl)}</span>`
          );
        }
        const v = ps.find((p) => p.seriesType === "bar");
        if (v)
          lines.push(
            `<span style="color:#6F8E38">vol</span> ${fmtUsd(v.data?.value ?? v.data)}`
          );
        for (const p of ps) {
          if (p.seriesType === "scatter" && p.data?.raw) {
            const e = p.data.raw;
            const st = EVENT_STYLE[e.event_type] || {};
            lines.push(
              `<div style="margin-top:5px;color:${st.color || "#6F8E38"}"><b>${e.event_type}</b> — ${st.verb || ""}</div>` +
                `<span style="color:#6F8E38">@</span> ${fmtPrice(e.price)}` +
                (e.frac ? ` · sold ${(e.frac * 100).toFixed(0)}%` : "") +
                (e.proceeds_usd != null ? ` · $${e.proceeds_usd.toFixed(2)}` : "")
            );
          }
        }
        return lines.join("<br/>");
      },
    },
    grid: [
      { left: 62, right: 96, top: 12, height: "66%" },
      { left: 62, right: 96, top: "76%", height: "16%" },
    ],
    xAxis: [
      {
        type: "category",
        gridIndex: 0,
        data: cats,
        boundaryGap: true,
        axisLine: { show: false },
        axisTick: { show: false },
        axisLabel: { show: false },
        splitLine: { show: false },
        axisPointer: { label: { show: false } },
      },
      {
        type: "category",
        gridIndex: 1,
        data: cats,
        boundaryGap: true,
        axisLine: { lineStyle: { color: "rgba(147,192,31,0.18)" } },
        axisTick: { show: false },
        axisLabel: {
          color: "#6F8E38",
          fontSize: 12,
          fontFamily: MONO,
          formatter: (v) => fmtAxisTime(v, itv),
          hideOverlap: true,
        },
        splitLine: { show: false },
        axisPointer: {
          label: {
            formatter: (p) => fmtAxisTime(p.value, itv === "1d" ? "1h" : itv),
          },
        },
      },
    ],
    yAxis: [
      {
        type: yLog ? "log" : "value",
        gridIndex: 0,
        scale: true,
        logBase: 10,
        // tighten the extent — a bare log axis snaps to powers of 10 and
        // wastes half the panel
        min: (e) => (isFinite(e.min) ? e.min * (yLog ? 0.75 : 0.98) : null),
        max: (e) => (isFinite(e.max) ? e.max * (yLog ? 1.25 : 1.02) : null),
        axisLine: { show: false },
        axisLabel: {
          color: "#6F8E38",
          fontSize: 12,
          fontFamily: MONO,
          formatter: (v) => fmtPrice(v),
        },
        splitLine: { lineStyle: { color: "rgba(147,192,31,0.07)", type: "dotted" } },
        axisPointer: { label: { formatter: (p) => fmtPrice(p.value) } },
      },
      {
        type: "value",
        gridIndex: 1,
        scale: false,
        axisLine: { show: false },
        axisTick: { show: false },
        axisLabel: { show: false },
        splitLine: { show: false },
        axisPointer: { show: false },
      },
    ],
    dataZoom: [{ type: "inside", xAxisIndex: [0, 1], start: 0, end: 100 }],
    series: [
      {
        name: "price",
        type: "candlestick",
        data: k,
        itemStyle: {
          color: "#3DDC84",
          color0: "#FF5147",
          borderColor: "#3DDC84",
          borderColor0: "#FF5147",
          borderWidth: 1,
        },
        markLine: {
          symbol: "none",
          silent: true,
          animation: false,
          data: ml,
          emphasis: { disabled: true },
        },
      },
      {
        name: "volume",
        type: "bar",
        xAxisIndex: 1,
        yAxisIndex: 1,
        data: vols,
        barWidth: "62%",
      },
      {
        name: "events",
        type: "scatter",
        data: evPts,
        z: 12,
        zlevel: 1,
      },
    ],
  };
}

// ── small UI atoms ──────────────────────────────────────────────────────────
function Pills({ options, value, onChange, labels, titles }) {
  return (
    <div className="flex items-center bg-black/30 border border-edge p-0.5 gap-0.5 shrink-0">
      {options.map((o) => (
        <button
          key={o}
          onClick={() => onChange(o)}
          title={titles && titles[o]}
          className={`px-2 py-[3px] text-[12px] font-bold tracking-[0.12em] uppercase ${
            value === o ? "bg-tail text-base" : "text-muted hover:text-ink hover:bg-live/[0.06]"
          }`}
        >
          {(labels && labels[o]) || o}
        </button>
      ))}
    </div>
  );
}

function Chip({ label, value, tone, sub, ignite = false }) {
  const color =
    tone === "win" ? "text-win" : tone === "loss" ? "text-loss" : tone === "live" ? "text-live" : "text-ink";
  return (
    <div
      className="bg-black/30 border border-edge/70 px-2.5 py-1.5 min-w-0"
      title={sub ? `${value} · ${sub}` : String(value)}
    >
      <div className="text-[11px] uppercase tracking-[0.14em] text-muted font-semibold truncate">
        {label}
      </div>
      <div className={`num font-semibold text-[15px] mt-px truncate ${color} ${ignite ? "ignite" : ""}`}>
        {value}
      </div>
      {sub && <div className="text-[12px] text-muted/70 truncate">{sub}</div>}
    </div>
  );
}

function ExtLink({ href, children }) {
  return (
    <a
      href={href}
      target="_blank"
      rel="noreferrer"
      onClick={(e) => e.stopPropagation()}
      className="text-[13px] text-muted hover:text-tail border border-edge hover:border-tail/40 rounded-md px-2 py-[3px] whitespace-nowrap"
    >
      {children}
    </a>
  );
}

// ── the terminal ────────────────────────────────────────────────────────────
export default function TokenTerminal({ mint, onClose }) {
  const [detail, setDetail] = useState(null);
  const [live, setLive] = useState(null);
  const [data, setData] = useState(null); // last good candles payload
  const [loading, setLoading] = useState(true);
  const [range, setRange] = useState("call");
  const [interval_, setInterval_] = useState("auto");
  const [yLog, setYLog] = useState(true);
  const [copied, setCopied] = useState(false);

  const el = useRef(null);
  const chart = useRef(null);
  const seq = useRef(0);

  const refreshDetail = React.useCallback(() => {
    fetch(withBook(`/api/token/${mint}`))
      .then((r) => (r.ok ? r.json() : null))
      .then((j) => j && setDetail(j))
      .catch(() => {});
  }, [mint]);

  // position + events (once per mint)
  useEffect(() => {
    setDetail(null);
    setData(null);
    setLive(null);
    refreshDetail();
  }, [mint, refreshDetail]);

  // live strip: poll every 4s while open
  useEffect(() => {
    let stop = false;
    const tick = () =>
      fetch(`/api/token/${mint}/live`)
        .then((r) => (r.ok ? r.json() : null))
        .then((j) => {
          if (!stop && j) setLive(j);
        })
        .catch(() => {});
    tick();
    const t = setInterval(tick, 4000);
    return () => {
      stop = true;
      clearInterval(t);
    };
  }, [mint]);

  // candles: refetch on range/interval change + poll every 30s
  useEffect(() => {
    let stop = false;
    const load = (showLoading) => {
      const id = ++seq.current;
      if (showLoading) setLoading(true);
      fetch(withBook(`/api/token/${mint}/candles?range=${range}&interval=${interval_}`))
        .then((r) => (r.ok ? r.json() : null))
        .then((j) => {
          if (stop || seq.current !== id) return;
          if (j) setData(j);
          setLoading(false);
        })
        .catch(() => {
          if (!stop && seq.current === id) setLoading(false);
        });
    };
    load(true);
    const t = setInterval(() => load(false), 30000);
    return () => {
      stop = true;
      clearInterval(t);
    };
  }, [mint, range, interval_]);

  // chart lifecycle — the container only exists once detail has loaded,
  // so (re)init whenever it appears
  useEffect(() => {
    if (!el.current || chart.current) return;
    chart.current = echarts.init(el.current, null, { renderer: "canvas" });
    const ro = new ResizeObserver(() => chart.current?.resize());
    ro.observe(el.current);
    return () => {
      ro.disconnect();
      chart.current?.dispose();
      chart.current = null;
    };
  }, [detail]);

  useEffect(() => {
    if (!chart.current || !data || !(data.candles || []).length) return;
    chart.current.setOption(buildOption(data, detail?.events, yLog), { notMerge: true });
  }, [data, detail, yLog]);

  const pos = detail?.position;
  const callPrice = pos?.signal_price;
  const price = live?.price ?? pos?.current_price ?? null;
  const priceIsLive = live?.price != null;
  const pctFromCall =
    price != null && callPrice ? (price / callPrice - 1) * 100 : null;
  const mult = pos?.realized_multiple ?? pos?.current_multiple;
  const peakMult =
    pos?.peak_price && pos?.entry_price ? pos.peak_price / pos.entry_price : null;
  // Position economics, derived honestly from the event log + the live price
  // (NOT from positions.realized_pnl_usd, which holds the engine's running mark).
  const sells = (detail?.events || []).filter((e) => e.proceeds_usd != null);
  const soldUsd = sells.reduce((s, e) => s + e.proceeds_usd, 0);
  const stake = pos?.stake_usd;
  const isClosed = !!pos?.closed_at;
  const bagUsd =
    !isClosed && stake != null && pos?.entry_price && pos?.remaining_frac != null && price != null
      ? stake * pos.remaining_frac * (price / pos.entry_price)
      : isClosed
      ? 0
      : null;
  const netPnl =
    isClosed && stake != null && pos?.realized_multiple != null
      ? stake * (pos.realized_multiple - 1)
      : stake != null && bagUsd != null
      ? soldUsd + bagUsd - stake
      : null;
  const heldH = pos?.entry_at
    ? ((pos.closed_at ? Date.parse(pos.closed_at) : Date.now()) - Date.parse(pos.entry_at)) / 3.6e6
    : null;
  const noCandles = !loading && (!data || !(data.candles || []).length);

  const copyMint = () => {
    navigator.clipboard?.writeText(mint).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1200);
    });
  };

  return (
    <div className="flex flex-col h-full min-h-0">
      {/* ── header ── */}
      <div className="flex items-center gap-3 px-5 pt-4 pb-3 border-b border-edge/60 shrink-0">
        <div className="flex items-center gap-2.5 min-w-0">
          <span className="text-[24px] font-bold tracking-tight text-ink leading-none">
            {pos?.ticker || mint.slice(0, 6)}
          </span>
          {/* the full-screen modal covers the page's LIVE/PAPER toggle — the buy/sell buttons
              below need their own unambiguous money-context cue (post-incident hardening) */}
          <span
            className={`mepola-badge px-1.5 text-[11px] font-bold tracking-[0.12em] ${
              currentBook() === "live" ? "text-loss" : "text-muted"
            }`}
            title={currentBook() === "live"
              ? "actions here move REAL money"
              : "practice book — simulated money"}
          >
            {currentBook() === "live" ? "LIVE · REAL $" : "PAPER · PRACTICE"}
          </span>
          {pos?.state && (
            <span
              className={`mepola-badge px-1 text-[12px] font-bold tracking-[0.1em] ${
                STATE_TONE[pos.state] || STATE_TONE.EXITED
              }`}
            >
              {pos.state}
            </span>
          )}
          <button
            onClick={copyMint}
            title="copy mint address"
            className="num text-[13px] text-muted hover:text-ink flex items-center gap-1.5 border border-edge hover:border-live/60 rounded-md px-2 py-[3px]"
          >
            {shortMint(mint)}
            {copied ? (
              <span className="text-win font-semibold">copied ✓</span>
            ) : (
              <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <rect x="9" y="9" width="13" height="13" rx="2" />
                <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
              </svg>
            )}
          </button>
          <ExtLink href={`https://dexscreener.com/solana/${mint}`}>DexScreener ↗</ExtLink>
          <ExtLink href={`https://solscan.io/token/${mint}`}>Solscan ↗</ExtLink>
        </div>
        <div className="flex-1" />
        {/* live strip */}
        <div className="flex items-center gap-4 shrink-0">
          <div className="text-right">
            <div className="flex items-center justify-end gap-2">
              <span
                className={`w-1.5 h-1.5 rounded-full ${priceIsLive ? "bg-win pulse-dot" : "bg-muted"}`}
              />
              <span className="num text-[23px] font-bold text-ink leading-none">
                ${fmtPrice(price)}
              </span>
            </div>
            <div className="text-[12px] text-muted mt-0.5 uppercase tracking-[0.12em] font-semibold">
              {priceIsLive ? "live price" : price != null ? "last known price" : "no price yet"}
            </div>
          </div>
          <div className="text-right">
            <div
              className={`num text-[16px] font-semibold leading-none ${
                pctFromCall == null ? "text-muted" : pctFromCall >= 0 ? "text-win" : "text-loss"
              }`}
            >
              {fmtSignedPct(pctFromCall, 0)}
            </div>
            <div className="text-[12px] text-muted mt-1 uppercase tracking-[0.12em] font-semibold">
              vs call
            </div>
          </div>
          <div className="text-right">
            <div className="num text-[16px] font-semibold text-ink leading-none">
              {fmtUsd(live?.liquidity)}
            </div>
            <div className="text-[12px] text-muted mt-1 uppercase tracking-[0.12em] font-semibold">
              liquidity
            </div>
          </div>
          <div className="text-right">
            <div
              className={`num text-[16px] font-semibold leading-none ${
                live?.price_change_24h == null
                  ? "text-muted"
                  : live.price_change_24h >= 0
                  ? "text-win"
                  : "text-loss"
              }`}
            >
              {fmtSignedPct(live?.price_change_24h)}
            </div>
            <div className="text-[12px] text-muted mt-1 uppercase tracking-[0.12em] font-semibold">
              24h
            </div>
          </div>
        </div>
        <button
          className="text-muted hover:text-ink text-xl leading-none px-1 ml-1 shrink-0"
          onClick={onClose}
        >
          ×
        </button>
      </div>

      {!detail ? (
        <div className="flex-1 flex items-center justify-center text-muted text-sm">
          <span className="ascii-spinner mr-2 text-live" aria-hidden="true" />
          LOADING▊
        </div>
      ) : (
        <>
          {/* ── chart controls ── */}
          <div className="flex items-center gap-2 px-5 pt-2.5 pb-1 shrink-0">
            <Pills
              options={["call", "24h", "max"]}
              labels={{ call: "since call" }}
              value={range}
              onChange={setRange}
            />
            <Pills
              options={["auto", "1m", "1h", "1d"]}
              value={interval_}
              onChange={setInterval_}
            />
            <div className="flex-1" />
            {loading && data && (
              <span className="text-[12px] text-muted/70 uppercase tracking-[0.14em] font-semibold animate-pulse">
                updating…
              </span>
            )}
            {data?.interval && (
              <span className="num text-[12px] text-muted/60">{data.interval} bars</span>
            )}
            <Pills
              options={["log", "linear"]}
              value={yLog ? "log" : "linear"}
              onChange={(v) => setYLog(v === "log")}
              titles={{
                log: "equal distance = equal % move — every 2× doubles the same height; right for tokens that move in multiples",
                linear: "equal distance = equal $ move — a 3× near the top dwarfs a 3× near the bottom",
              }}
            />
          </div>

          {/* ── chart ── */}
          <div className="relative h-[55%] shrink-0 px-1">
            <div ref={el} className={`w-full h-full ${noCandles ? "invisible" : ""}`} />
            {loading && !data && (
              <div className="absolute inset-3 rounded-xl bg-live/[0.04] animate-pulse" />
            )}
            {noCandles && (
              <div className="absolute inset-0 flex flex-col items-center justify-center gap-1.5 text-center px-8">
                <div className="text-[16px] text-muted">
                  NO CHART DATA INDEXED YET▊ usually within a few minutes for new tokens
                </div>
                {price != null && (
                  <div className="num text-[14px] text-muted/70">
                    live price ${fmtPrice(price)}
                    {callPrice ? ` · ${fmtSignedPct(pctFromCall, 0)} vs call` : ""}
                  </div>
                )}
              </div>
            )}
          </div>

          {/* ── override actions (buy now / sell / close / TP / SL / trailing) — both books:
                LIVE = real orders; PAPER = practice orders the twin fills with simulated money ── */}
          <TokenActions mint={mint} pos={pos} live={live} onAction={refreshDetail} />

          {/* ── position chips + lifecycle ── */}
          <div className="flex gap-4 px-5 pt-2.5 pb-4 border-t border-edge/60 flex-1 min-h-0">
            {pos ? (
              <div className="grid grid-cols-4 gap-1.5 w-[400px] shrink-0 content-start">
                <Chip
                  label="stake"
                  value={pos.stake_usd != null ? "$" + pos.stake_usd.toFixed(2) : "—"}
                />
                <Chip
                  label="entry"
                  value={pos.entry_price ? "$" + fmtPrice(pos.entry_price) : "—"}
                  sub={pos.entry_at ? pos.entry_at.slice(5, 16).replace("T", " ") : "never entered"}
                />
                <Chip
                  label="multiple"
                  value={mult ? mult.toFixed(2) + "×" : "—"}
                  tone={mult ? (mult >= 1 ? "win" : "loss") : undefined}
                  ignite={!!igniteClass(mult)}
                />
                <Chip
                  label="peak"
                  value={peakMult ? peakMult.toFixed(1) + "×" : "—"}
                  tone="live"
                  ignite={!!igniteClass(peakMult)}
                />
                <Chip
                  label="bag left"
                  value={
                    pos.remaining_frac != null
                      ? Math.round(pos.remaining_frac * 100) + "%"
                      : "—"
                  }
                  sub={pos.secured ? "stake secured ✓" : "not secured"}
                />
                <Chip
                  label="stop"
                  value={
                    pos.secured
                      ? "removed"
                      : pos.stop_price
                      ? "$" + fmtPrice(pos.stop_price)
                      : "—"
                  }
                  tone={pos.secured ? undefined : "loss"}
                />
                <Chip
                  label="next sell"
                  value={
                    !isClosed && pos.next_rung_mult && pos.entry_price
                      ? pos.next_rung_mult.toFixed(0) + "×"
                      : "—"
                  }
                  sub={
                    !isClosed && pos.next_rung_price
                      ? "at $" + fmtPrice(pos.next_rung_price)
                      : undefined
                  }
                  tone="live"
                />
                <Chip
                  label="held"
                  value={
                    heldH == null
                      ? "—"
                      : heldH < 1
                      ? Math.max(1, Math.round(heldH * 60)) + "m"
                      : heldH < 48
                      ? heldH.toFixed(1) + "h"
                      : (heldH / 24).toFixed(1) + "d"
                  }
                />
                <Chip
                  label="sold so far"
                  value={stake != null ? "$" + soldUsd.toFixed(2) : "—"}
                  sub={`${sells.length} sell${sells.length === 1 ? "" : "s"} banked`}
                />
                <Chip
                  label="bag now"
                  value={bagUsd != null ? "$" + bagUsd.toFixed(2) : "—"}
                  sub={isClosed ? "position closed" : "remaining × live price"}
                />
                <Chip
                  label="net p&l"
                  value={
                    netPnl != null
                      ? (netPnl >= 0 ? "+$" : "−$") + Math.abs(netPnl).toFixed(2)
                      : "—"
                  }
                  sub="sold + bag − stake"
                  tone={netPnl != null ? (netPnl >= 0 ? "win" : "loss") : undefined}
                />
                <Chip
                  label="close"
                  value={pos.close_reason ? pos.close_reason.replace(/_/g, " ") : "open"}
                  sub={pos.closed_at ? pos.closed_at.slice(5, 16).replace("T", " ") : undefined}
                />
              </div>
            ) : (
              <div className="w-[400px] shrink-0 text-[14px] text-muted">no position record</div>
            )}
            <div className="flex-1 min-w-0 flex flex-col">
              <div className="tile-label pb-1.5 shrink-0">lifecycle</div>
              <div className="ml-1 space-y-[3px] text-[13px] overflow-auto min-h-0">
                {(detail.events || []).map((e, i, arr) => {
                  const st = EVENT_STYLE[e.event_type] || EVENT_STYLE.FINALIZE;
                  return (
                    <div key={e.id} className="flex gap-2.5 items-baseline py-px">
                      {/* the lifecycle rail is a literal tree now: ├─ … └─ */}
                      <span className="num shrink-0 select-none" style={{ color: st.color }} aria-hidden="true">
                        {i === arr.length - 1 ? "└─" : "├─"}
                      </span>
                      <span className="num text-muted/70 w-[104px] shrink-0">
                        {e.ts?.slice(5, 16).replace("T", " ")}
                      </span>
                      <span
                        className="num w-[74px] shrink-0 font-semibold"
                        style={{ color: st.color }}
                      >
                        {e.event_type}
                      </span>
                      {e.price != null && (
                        <span className="num text-muted/80 w-[92px] shrink-0">
                          ${fmtPrice(e.price)}
                        </span>
                      )}
                      <span className="text-muted truncate">{e.note}</span>
                    </div>
                  );
                })}
              </div>
            </div>
          </div>
        </>
      )}
    </div>
  );
}
