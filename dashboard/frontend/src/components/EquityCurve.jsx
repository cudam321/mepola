import React, { useEffect, useRef } from "react";
import * as echarts from "echarts";

const MONO = 'DepartureMono, "Pixelify Sans", ui-monospace, monospace';

// One tab at a time — LIVE (your paper account, phosphor) or BACKTEST (the seed
// replay, red/green). The two are never drawn together: mixing "your money"
// with "the evidence" is exactly what made the old chart confusing.
export default function EquityCurve({ snapshot, mode = "live" }) {
  const el = useRef(null);
  const chart = useRef(null);
  const dataSig = useRef(null); // signature of the plotted data — skip identical snapshots

  useEffect(() => {
    chart.current = echarts.init(el.current, null, { renderer: "canvas" });
    const ro = new ResizeObserver(() => chart.current?.resize());
    ro.observe(el.current);
    return () => { ro.disconnect(); chart.current?.dispose(); };
  }, []);

  const eq = snapshot?.equity || {};
  const live = eq.live || [];
  const seedFixed = eq.seed_fixed || [];
  const seedFrac = eq.seed_frac || [];
  const start = snapshot?.meta?.bankroll_start_usd || 500;
  const liveEmpty = mode === "live" && live.length < 2;

  useEffect(() => {
    if (!chart.current || !snapshot) return;

    // SIGNATURE SKIP: the WS pushes a snapshot every ~2s but these series only
    // change when a trade closes / the heartbeat lands — don't re-animate on noise.
    const sig = JSON.stringify([mode, start, live.length, seedFixed.length, seedFrac.length,
      live[live.length - 1], seedFixed[seedFixed.length - 1], seedFrac[seedFrac.length - 1]]);
    if (sig === dataSig.current) return;
    dataSig.current = sig;

    const startLine = {
      symbol: "none",
      silent: true,
      animation: false,
      data: [
        {
          yAxis: start,
          lineStyle: { color: "rgba(166,214,60,0.25)", type: "dashed" },
          label: {
            formatter: "── START $" + start + " ──",
            position: "insideEndTop",
            color: "#6F8E38",
            fontSize: 13,
            fontFamily: MONO,
          },
        },
      ],
    };

    const series =
      mode === "live"
        ? [
            {
              name: "paper balance",
              type: "line",
              data: live,
              showSymbol: false,
              sampling: "lttb",
              lineStyle: { color: "#CBF14E", width: 2 },
              areaStyle: {
                color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
                  { offset: 0, color: "rgba(203,241,78,0.14)" },
                  { offset: 1, color: "rgba(203,241,78,0)" },
                ]),
              },
              markLine: startLine,
            },
          ]
        : [
            {
              name: "$3-fixed (what we trade)",
              type: "line",
              data: seedFixed,
              showSymbol: false,
              sampling: "lttb",
              lineStyle: { color: "#FF5147", width: 1.5 },
              areaStyle: {
                color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
                  { offset: 0, color: "rgba(255,81,71,0.12)" },
                  { offset: 1, color: "rgba(255,81,71,0)" },
                ]),
              },
              markLine: startLine,
            },
            {
              name: "0.6%-fractional (survivable)",
              type: "line",
              data: seedFrac,
              showSymbol: false,
              sampling: "lttb",
              lineStyle: { color: "#3DDC84", width: 1.5 },
              areaStyle: {
                color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
                  { offset: 0, color: "rgba(61,220,132,0.07)" },
                  { offset: 1, color: "rgba(61,220,132,0)" },
                ]),
              },
            },
          ];

    chart.current.setOption(
      {
        backgroundColor: "transparent",
        grid: { left: 52, right: 16, top: 14, bottom: 24 },
        tooltip: {
          trigger: "axis",
          backgroundColor: "rgba(6,10,5,0.96)",
          borderColor: "rgba(147,192,31,0.55)",
          borderWidth: 1,
          textStyle: { color: "#A6D63C", fontSize: 14, fontFamily: MONO },
          extraCssText:
            "border-radius:2px;box-shadow:0 8px 30px rgba(0,0,0,.5);padding:10px 12px;",
          valueFormatter: (v) => (v == null ? "—" : "$" + v.toFixed(0)),
        },
        xAxis: {
          type: "time",
          axisLine: { lineStyle: { color: "rgba(147,192,31,0.18)" } },
          axisLabel: { color: "#6F8E38", fontSize: 13, fontFamily: MONO },
          splitLine: { show: false },
        },
        yAxis: {
          type: "value",
          axisLine: { lineStyle: { color: "rgba(147,192,31,0.18)" } },
          axisLabel: { color: "#6F8E38", fontSize: 13, fontFamily: MONO, formatter: "${value}" },
          splitLine: { lineStyle: { color: "rgba(147,192,31,0.07)", type: "dotted" } },
        },
        series,
      },
      { notMerge: true }
    );
  }, [snapshot, mode]);

  // relative/overflow-hidden parent + absolute-inset chart: the canvas can never
  // influence layout size, which kills the resize->grow->resize feedback loop
  // that made this panel expand horizontally forever (2026-07-03).
  return (
    <div className="relative w-full h-full overflow-hidden">
      <div ref={el} className="absolute inset-0" />
      {liveEmpty && (
        <div className="absolute inset-0 flex items-center justify-center text-[14px] text-muted/80 text-center px-6 leading-relaxed pointer-events-none">
          AWAITING FIRST CLOSE▊ your paper account starts at ${start} — the line begins with the
          first closed trade.
        </div>
      )}
    </div>
  );
}
