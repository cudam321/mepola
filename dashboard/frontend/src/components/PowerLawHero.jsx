import React, { useEffect, useRef } from "react";
import * as echarts from "echarts";

const WIN = "#3DDC84";
const LOSS = "#FF5147";
const LIVE = "#CBF14E";
const TAIL = "#CBF14E";
const PARETO = "#93C01F"; // the phosphor — cumulative-gains reference dash
const MUTED = "#6F8E38";
const INK = "#A6D63C";
// the ONE incandescent treatment — RESERVED for ≥10x tail elements only
const INCANDESCENT = "#F6FFE1";
// the ideal-curve ghost: dim phosphor, never glowing, reads as annotation
const GHOST = "rgba(147,192,31,0.35)";
const GHOST_LABEL = "rgba(147,192,31,0.6)";
const FLOOR = 0.01;

const MONO = 'DepartureMono, "Pixelify Sans", ui-monospace, monospace';
const TOOLTIP = {
  backgroundColor: "rgba(6,10,5,0.96)",
  borderColor: "rgba(147,192,31,0.55)",
  borderWidth: 1,
  textStyle: { color: INK, fontSize: 15, fontFamily: MONO },
  extraCssText:
    "border-radius:2px;box-shadow:0 8px 30px rgba(0,0,0,.6);padding:10px 12px;",
};

function sizeFromPnl(pnl) {
  const a = Math.abs(pnl || 0);
  return Math.max(5, Math.min(46, 5 + Math.sqrt(a) * 2.2));
}
function fmtX(v) {
  if (v >= 1000) return v.toFixed(0) + "x";
  if (v >= 1) return v.toFixed(v >= 10 ? 0 : 1) + "x";
  return v.toFixed(2) + "x";
}

// Per-point style: a ≥10x winner uses the RESERVED color + a reserved halo +
// bright border (the sacred patch); gains stay green, losers thin + dimmed.
function styleFor(p, isTop) {
  if (p.multiple >= 10) {
    return {
      color: INCANDESCENT,
      opacity: 1,
      shadowBlur: isTop ? 40 : 24,
      shadowColor: "rgba(147,192,31,0.85)",
      borderColor: "rgba(246,255,225,0.95)",
      borderWidth: isTop ? 1.8 : 1.1,
    };
  }
  if (p.multiple > 1) {
    return { color: WIN, opacity: 0.85, borderColor: "rgba(0,0,0,0.35)" };
  }
  return { color: LOSS, opacity: 0.35, borderWidth: 0 };
}

// Filter by source (live/seed) and honestly re-rank + recompute the cumulative
// share of gains over the filtered set (server values are for the full set only).
function scopeHero(all, scope) {
  if (!scope || scope === "all") return all;
  const scoped = all
    .filter((p) => p.source === scope)
    .sort((a, b) => b.multiple - a.multiple);
  const gains = scoped.reduce((t, p) => t + Math.max(p.pnl || 0, 0), 0);
  let cum = 0;
  return scoped.map((p, i) => {
    cum += Math.max(p.pnl || 0, 0);
    return { ...p, rank: i + 1, cum_pct: gains > 0 ? +((cum / gains) * 100).toFixed(3) : 0 };
  });
}

// The classic textbook curve: ideal(r) = best · r^(−1/α), anchored so rank 1
// hits the observed top; stop drawing once it sinks below the chart floor.
function idealCurve(best, alpha, N, yMin) {
  if (!best || best <= 0 || !alpha || alpha <= 0) return [];
  const pts = [];
  for (let r = 1; r <= N; r++) {
    const v = best * Math.pow(r, -1 / alpha);
    if (v < yMin) break;
    pts.push([r, v]);
  }
  return pts;
}

// small mono label pinned near the curve's mid-course — annotation, not data
// (a markPoint because line labels don't render with showSymbol:false)
function idealMarkPoint(ideal, alpha) {
  const mid = ideal[Math.floor(ideal.length / 2)];
  if (!mid) return { data: [] };
  return {
    silent: true,
    animation: false,
    symbol: "circle",
    symbolSize: 0.1,
    data: [
      {
        coord: mid,
        label: {
          show: true,
          position: "top",
          distance: 8,
          formatter: `IDEAL POWER LAW α=${alpha.toFixed(2)}`,
          color: GHOST_LABEL,
          fontSize: 13,
          fontFamily: MONO,
        },
      },
    ],
  };
}

// Reference lines/areas are STATIC decor: animation:false so they never re-animate,
// even if a merge update happens to touch them.
function markLineData(showGraveyard) {
  return [
    {
      yAxis: 1,
      lineStyle: { color: INK, width: 2, type: "solid" },
      label: { formatter: "── BREAK-EVEN 1.0x ──", fontSize: 13, fontFamily: MONO },
    },
    {
      yAxis: 10,
      lineStyle: { color: TAIL, width: 1, type: "dashed" },
      label: { formatter: "── TAIL ≥10x ──", color: TAIL, fontSize: 13, fontFamily: MONO },
    },
    ...(showGraveyard
      ? [{
          yAxis: FLOOR,
          lineStyle: { color: "rgba(147,192,31,0.2)", width: 1, type: "dotted" },
          label: { formatter: "── TOTAL LOSS (0x) ──", color: "rgba(111,142,56,0.85)", fontSize: 12, fontFamily: MONO },
        }]
      : []),
  ];
}

function markAreaData(yMax) {
  return [
    [
      {
        yAxis: 0.1,
        itemStyle: { color: "rgba(255,81,71,0.04)" },
        label: {
          show: true,
          position: "insideTop",
          color: "rgba(255,81,71,0.5)",
          fontSize: 12,
          fontFamily: MONO,
          formatter: "░░ THE BLEED (AS DESIGNED) ░░",
        },
      },
      { yAxis: 1 },
    ],
    [
      {
        yAxis: 10,
        itemStyle: { color: "rgba(203,241,78,0.03)" },
        label: {
          show: true,
          position: "insideBottom",
          color: "rgba(203,241,78,0.55)",
          fontSize: 12,
          fontFamily: MONO,
          formatter: "░░ THE TAIL — THE WHOLE EDGE LIVES HERE ░░",
        },
      },
      { yAxis: yMax },
    ],
  ];
}

// The full option = static shell (axes, marks, labels, series shells) + current data.
// Built with notMerge:true ONLY on structural changes; data-only ticks go through
// a small MERGE setOption that never touches the shell.
function fullOption({ scope, N, yMin, yMax, showGraveyard, isEmpty, closed, live, pareto, ideal, alpha, daysTxt }) {
  const option = {
    backgroundColor: "transparent",
    animationDurationUpdate: 600,
    animationEasingUpdate: "cubicOut",
    grid: { left: 62, right: 62, top: 30, bottom: 46 },
    tooltip: {
      trigger: "item",
      ...TOOLTIP,
      formatter: (pm) => {
        if (pm.seriesName === "ideal")
          return (
            "<b>IDEAL POWER LAW</b> · ideal(r) = best · r^(−1/α)<br/>" +
            "textbook Pareto with the measured tail exponent —<br/>compare the real curve against it"
          );
        const r = pm.data.raw;
        if (!r) return `top ${pm.data[0]} = ${pm.data[1].toFixed(1)}% of gains`;
        const m =
          r.multiple < 0.1
            ? `${r.multiple.toFixed(3)}x (total loss, floored)`
            : fmtX(r.multiple);
        const kind = r.kind === "unrealized" ? "LIVE (unrealized)" : r.state;
        return `<b>${r.ticker}</b> · ${kind}<br/>multiple: <b${
          r.multiple >= 10 ? ' class="ignite"' : ""
        }>${m}</b><br/>P&L: ${
          r.pnl >= 0 ? "+" : ""
        }$${r.pnl.toFixed(2)}<br/>rank ${r.rank} · cum ${r.cum_pct}% of gains`;
      },
    },
    xAxis: {
      type: "value",
      min: 1,
      max: N,
      name: "positions ranked by return multiple  →",
      nameLocation: "middle",
      nameGap: 28,
      nameTextStyle: { color: MUTED, fontSize: 13, fontFamily: MONO },
      axisLine: { lineStyle: { color: "rgba(147,192,31,0.2)" } },
      splitLine: { show: false },
      axisLabel: { color: MUTED, fontSize: 13, fontFamily: MONO },
    },
    yAxis: [
      {
        type: "log",
        logBase: 10,
        min: yMin,
        max: yMax,
        name: "return multiple (log)",
        nameTextStyle: { color: MUTED, fontSize: 13, fontFamily: MONO, align: "left" },
        axisLine: { lineStyle: { color: "rgba(147,192,31,0.2)" } },
        splitLine: { lineStyle: { color: "rgba(147,192,31,0.08)", type: "dotted" } },
        axisLabel: { color: MUTED, fontSize: 13, fontFamily: MONO, formatter: fmtX },
      },
      {
        type: "value",
        min: 0,
        max: 100,
        position: "right",
        name: "cum % of gains",
        nameTextStyle: { color: MUTED, fontSize: 13, fontFamily: MONO },
        axisLine: { show: true, lineStyle: { color: "rgba(147,192,31,0.16)" } },
        splitLine: { show: false },
        axisLabel: {
          color: PARETO,
          fontSize: 13,
          fontFamily: MONO,
          formatter: "{value}%",
        },
      },
    ],
    series: [
      {
        name: "closed",
        type: "scatter",
        yAxisIndex: 0,
        clip: false,
        data: closed,
        symbolSize: (v, p) => sizeFromPnl(p.data.raw?.pnl),
        markLine: {
          symbol: "none",
          silent: true,
          animation: false,
          label: { color: INK, fontSize: 13, position: "insideEndTop" },
          data: markLineData(showGraveyard),
        },
        markArea: {
          silent: true,
          animation: false,
          data: markAreaData(yMax),
        },
      },
      {
        name: "live",
        type: "effectScatter",
        yAxisIndex: 0,
        clip: false,
        data: live,
        symbolSize: (v, p) => sizeFromPnl(p.data.raw?.pnl),
        rippleEffect: { scale: 3, brushType: "stroke" },
        itemStyle: { color: LIVE, opacity: 0.95 },
        z: 5,
      },
      {
        name: "pareto",
        type: "line",
        yAxisIndex: 1,
        data: pareto,
        smooth: true,
        showSymbol: false,
        lineStyle: {
          color: PARETO,
          width: 1.5,
          type: "dashed",
          opacity: 0.9,
        },
        tooltip: { show: false },
        z: 2,
      },
      {
        // the sanctioned reference annotation: thin dashed ghost, no glow, under everything
        name: "ideal",
        type: "line",
        yAxisIndex: 0,
        data: ideal,
        smooth: false,
        showSymbol: false,
        lineStyle: { color: GHOST, width: 1, type: [4, 4] },
        emphasis: { lineStyle: { width: 1, color: GHOST } },
        markPoint: idealMarkPoint(ideal, alpha),
        z: 1,
      },
    ],
    graphic: [
      {
        id: "days-text",
        type: "text",
        right: 74,
        top: 56,
        style: {
          text: daysTxt,
          fill: MUTED,
          fontSize: 14,
          fontFamily: MONO,
        },
      },
    ],
  };

  if (isEmpty) {
    option.graphic.push({
      id: "empty-text",
      type: "text",
      left: "center",
      top: "middle",
      style: {
        text:
          "   ((( · )))\n" +
          "      │\n" +
          "     ╱│╲\n" +
          " ▔▔▔▔▔▔▔▔▔▔▔\n\n" +
          (scope === "live"
            ? "NO LIVE TRADES — THE ENGINE IS WATCHING▊\nthe seed distribution is under SEED."
            : "NO POSITIONS YET — AWAITING FIRST TRADE▊\nWhen trades open they appear here — expect most below 1.0x (the bleed);\nthe edge depends on rarely catching one tail (ANSEM ≈197x)."),
        fill: "rgba(111,142,56,0.9)",
        fontSize: 16,
        lineHeight: 22,
        textAlign: "center",
        fontFamily: MONO,
      },
    });
  }
  return option;
}

export default function PowerLawHero({ snapshot, scope = "all", onSelect }) {
  const el = useRef(null);
  const chart = useRef(null);
  const dataSig = useRef(null);   // signature of everything that reaches the canvas
  const structSig = useRef(null); // signature of the static shell context
  const axisRange = useRef(null); // last applied [yMin, yMax]
  const onSelectRef = useRef(onSelect);
  onSelectRef.current = onSelect;

  useEffect(() => {
    chart.current = echarts.init(el.current, null, { renderer: "canvas" });
    // Bind the click handler ONCE — echarts keeps handlers across setOption calls,
    // and the ref always points at the latest onSelect without rebinding.
    chart.current.on("click", (p) => {
      const mint = p.data?.raw?.mint;
      if (mint && onSelectRef.current) onSelectRef.current(mint);
    });
    const ro = new ResizeObserver(() => chart.current?.resize());
    ro.observe(el.current);
    return () => { ro.disconnect(); chart.current?.dispose(); };
  }, []);

  useEffect(() => {
    if (!chart.current || !snapshot) return;
    const hero = scopeHero(snapshot.hero || [], scope);
    const stats = (snapshot.stats || {})[scope] || snapshot.stats || {};
    const days = stats.days_since_last_10x;
    // ideal-curve inputs: the measured tail exponent + the observed top anchor. F40: prefer
    // the SCOPED hill_alpha (the scoped stats object now carries it) so the ideal curve's
    // exponent matches the scope being viewed; fall back to the all-data alpha, then 1.4.
    const alpha = Number(stats.hill_alpha ?? snapshot.stats?.hill_alpha) || 1.4;
    const topMult = Math.max(...hero.map((p) => p.multiple), 0);

    // 1) SIGNATURE SKIP — the server pushes a snapshot every ~2s even when nothing
    // traded; if nothing that affects this chart changed, do NOTHING (no setOption).
    // rank/cum_pct/sizes/styles all derive from these fields, so they are covered;
    // alpha + the rank-1 anchor are included so the ideal curve redraws when they change.
    const sig = JSON.stringify([
      scope,
      hero.map((p) => [p.mint, p.multiple, p.pnl, p.state, p.kind]),
      stats.best ?? null,     // F53: the scoped stats object's key is `best`, not `best_multiple`
      days ?? null,
      alpha,
      topMult,
    ]);
    if (sig === dataSig.current) return;
    dataSig.current = sig;

    const toPoint = (p) => ({
      value: [p.rank, Math.max(p.multiple, FLOOR)],
      raw: p,
      symbol: p.multiple < 0.1 ? "diamond" : "circle",
      itemStyle: styleFor(p, p.multiple === topMult && p.multiple >= 10),
    });
    const closed = hero.filter((p) => p.kind === "realized").map(toPoint);
    const live = hero.filter((p) => p.kind === "unrealized").map(toPoint);
    const pareto = hero.map((p) => [p.rank, p.cum_pct]);
    const N = Math.max(hero.length, 1);
    // keep the ≥10x tail band in frame even when nothing has hit it yet
    const yMax = Math.max(15, (topMult || 1) * 1.6);
    // Dynamic floor: when nothing sits near total-loss, reclaim the dead bottom of the log
    // axis; the moment a real ~0x lands, the floor drops back to 0.01 and the graveyard shows.
    const minMult = hero.length
      ? Math.min(...hero.map((p) => Math.max(p.multiple, FLOOR)))
      : FLOOR;
    const yMin = minMult <= 0.12 ? FLOOR : Math.min(0.3, minMult * 0.55);
    const showGraveyard = yMin <= FLOOR * 1.01;
    const isEmpty = closed.length === 0 && live.length === 0;
    const ideal = isEmpty ? [] : idealCurve(topMult, alpha, N, yMin);
    const daysTxt =
      days == null
        ? "no ≥10x yet  ·  waiting for the tail"
        : days < 1
        ? `tail hit ${Math.max(1, Math.round(days * 24))}h ago`
        : `${days}d since last ≥10x  ·  waiting for the tail`;

    // 2) STATIC/DYNAMIC SPLIT — full notMerge rebuild ONLY when the shell context
    // changes (mount, scope switch, x-extent, graveyard toggle, empty-state text).
    // yMin/yMax changes go through the MERGE path below: they can move on any tick
    // (a live extreme point drifting), and a full rebuild there would re-animate
    // every reference element — the exact churn this fixes.
    const structKey = JSON.stringify([scope, N, showGraveyard, isEmpty]);
    if (structKey !== structSig.current) {
      structSig.current = structKey;
      axisRange.current = [yMin, yMax];
      chart.current.setOption(
        fullOption({ scope, N, yMin, yMax, showGraveyard, isEmpty, closed, live, pareto, ideal, alpha, daysTxt }),
        { notMerge: true, lazyUpdate: true }
      );
      return;
    }

    // Data-only tick: MERGE update — markLines/areas/axis labels untouched,
    // only the points/pareto/ideal glide (animationDurationUpdate 600) + the days text.
    const update = {
      series: [
        { data: closed },
        { data: live },
        { data: pareto },
        { data: ideal, markPoint: idealMarkPoint(ideal, alpha) },
      ],
      graphic: [{ id: "days-text", style: { text: daysTxt } }],
    };
    const [prevMin, prevMax] = axisRange.current || [];
    if (yMin !== prevMin || yMax !== prevMax) {
      axisRange.current = [yMin, yMax];
      update.yAxis = [{ min: yMin, max: yMax }, {}];
      // the tail band's top bound tracks yMax; animation:false keeps this silent
      update.series[0].markArea = { data: markAreaData(yMax) };
    }
    chart.current.setOption(update, { lazyUpdate: true });
  }, [snapshot, scope]);

  return <div ref={el} className="w-full h-full" />;
}
