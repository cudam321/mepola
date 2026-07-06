import React, { useEffect, useRef } from "react";
import * as echarts from "echarts";

const WIN = "#3DDC84";
const LOSS = "#FF5147";
const MONO = 'DepartureMono, "Pixelify Sans", ui-monospace, monospace';

// Compact daily realized-P&L bars (green up / red down).
export default function DailyPnl({ data }) {
  const el = useRef(null);
  const chart = useRef(null);
  const rows = data || [];

  useEffect(() => {
    chart.current = echarts.init(el.current, null, { renderer: "canvas" });
    const ro = new ResizeObserver(() => chart.current?.resize());
    ro.observe(el.current);
    return () => { ro.disconnect(); chart.current?.dispose(); };
  }, []);

  useEffect(() => {
    if (!chart.current) return;
    chart.current.setOption(
      {
        backgroundColor: "transparent",
        grid: { left: 38, right: 4, top: 10, bottom: 20 },
        tooltip: {
          trigger: "axis",
          backgroundColor: "rgba(6,10,5,0.96)",
          borderColor: "rgba(147,192,31,0.55)",
          borderWidth: 1,
          textStyle: { color: "#A6D63C", fontSize: 14, fontFamily: MONO },
          extraCssText:
            "border-radius:2px;box-shadow:0 8px 30px rgba(0,0,0,.5);padding:10px 12px;",
          formatter: (ps) => {
            const p = ps[0];
            const r = rows[p.dataIndex];
            const v = r?.realized_pnl || 0;
            return `<b>${r?.date}</b><br/>realized ${v >= 0 ? "+" : "−"}$${Math.abs(v).toFixed(
              2
            )}<br/>${r?.n_closed ?? 0} closed`;
          },
        },
        xAxis: {
          type: "category",
          data: rows.map((r) => String(r.date).slice(5)),
          axisLine: { lineStyle: { color: "rgba(147,192,31,0.18)" } },
          axisTick: { show: false },
          axisLabel: { color: "#6F8E38", fontSize: 12, fontFamily: MONO },
        },
        yAxis: {
          type: "value",
          axisLine: { show: false },
          axisLabel: {
            color: "#6F8E38",
            fontSize: 12,
            fontFamily: MONO,
            formatter: (v) => (v < 0 ? "-$" + Math.abs(v) : "$" + v),
          },
          splitLine: { lineStyle: { color: "rgba(147,192,31,0.07)", type: "dotted" } },
        },
        series: [
          {
            type: "bar",
            data: rows.map((r) => ({
              value: r.realized_pnl,
              itemStyle: {
                color: r.realized_pnl >= 0 ? WIN : LOSS,
                opacity: 0.85,
                borderRadius: 0,
              },
            })),
            barMaxWidth: 16,
            markLine: {
              symbol: "none",
              silent: true,
              label: { show: false },
              data: [{ yAxis: 0, lineStyle: { color: "rgba(166,214,60,0.25)", width: 1 } }],
            },
          },
        ],
      },
      { notMerge: true }
    );
  }, [data]);

  return (
    <div className="relative w-full h-full overflow-hidden">
      <div ref={el} className="absolute inset-0" />
      {rows.length === 0 && (
        <div className="absolute inset-0 flex items-center justify-center text-[14px] text-muted/80 text-center px-3 leading-relaxed">
          NO CLOSED TRADES▊ daily realized P&amp;L lands here.
        </div>
      )}
    </div>
  );
}
