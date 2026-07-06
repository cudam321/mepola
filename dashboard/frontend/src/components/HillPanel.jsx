import React, { useEffect, useRef } from "react";
import * as echarts from "echarts";

const MONO = 'DepartureMono, "Pixelify Sans", ui-monospace, monospace';

// Log-log CCDF (survival curve) of the return multiples. A straight line on log-log = power law.
export default function HillPanel({ snapshot }) {
  const el = useRef(null);
  const chart = useRef(null);
  const dataSig = useRef(null); // signature of the plotted data — skip identical snapshots

  useEffect(() => {
    chart.current = echarts.init(el.current, null, { renderer: "canvas" });
    const ro = new ResizeObserver(() => chart.current?.resize());
    ro.observe(el.current);
    return () => { ro.disconnect(); chart.current?.dispose(); };
  }, []);

  useEffect(() => {
    if (!chart.current || !snapshot?.ccdf) return;
    const { curve } = snapshot.ccdf;
    const data = curve
      .filter((d) => d.x > 0 && d.p > 0)
      .map((d) => [d.x, d.p]);

    // SIGNATURE SKIP: the CCDF only changes when a trade closes; don't rebuild
    // (and re-animate) the chart on every ~2s snapshot push.
    const sig = JSON.stringify(data);
    if (sig === dataSig.current) return;
    dataSig.current = sig;

    chart.current.setOption(
      {
        backgroundColor: "transparent",
        grid: { left: 46, right: 14, top: 34, bottom: 30 },
        tooltip: {
          trigger: "item",
          backgroundColor: "rgba(6,10,5,0.96)",
          borderColor: "rgba(147,192,31,0.55)",
          borderWidth: 1,
          textStyle: { color: "#A6D63C", fontSize: 14, fontFamily: MONO },
          extraCssText:
            "border-radius:2px;box-shadow:0 8px 30px rgba(0,0,0,.5);padding:10px 12px;",
          formatter: (p) => `≥ ${p.data[0].toFixed(2)}x : ${(p.data[1] * 100).toFixed(1)}% of trades`,
        },
        xAxis: {
          type: "log",
          name: "multiple",
          nameTextStyle: { color: "rgba(111,142,56,0.75)", fontSize: 13 },
          nameLocation: "middle",
          nameGap: 18,
          axisLine: { lineStyle: { color: "rgba(147,192,31,0.18)" } },
          axisLabel: { color: "#6F8E38", fontSize: 12, fontFamily: MONO, formatter: (v) => v + "x" },
          splitLine: { lineStyle: { color: "rgba(147,192,31,0.07)", type: "dotted" } },
        },
        yAxis: {
          type: "log",
          name: "P(≥x)",
          nameTextStyle: { color: "rgba(111,142,56,0.75)", fontSize: 13 },
          axisLine: { lineStyle: { color: "rgba(147,192,31,0.18)" } },
          axisLabel: { color: "#6F8E38", fontSize: 12, fontFamily: MONO },
          splitLine: { lineStyle: { color: "rgba(147,192,31,0.07)", type: "dotted" } },
        },
        series: [
          {
            type: "scatter",
            data,
            symbolSize: 4,
            itemStyle: {
              color: "#CBF14E",
              opacity: 0.75,
            },
          },
        ],
      },
      { notMerge: true }
    );
  }, [snapshot]);

  const alpha = snapshot?.ccdf?.alpha;
  return (
    <div className="relative w-full h-full">
      <div ref={el} className="w-full h-full" />
      <div className="absolute top-0 right-1 num text-[13px] px-1.5 py-0.5 rounded-md bg-live/[0.05] border border-edge text-tail">
        Hill α ≈ {alpha ? alpha.toFixed(2) : "—"}
      </div>
    </div>
  );
}
