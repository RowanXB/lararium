import { LineChart } from "echarts/charts";
import { GridComponent, TooltipComponent } from "echarts/components";
import * as echarts from "echarts/core";
import { CanvasRenderer } from "echarts/renderers";
import { useEffect, useRef } from "react";

import type { NetWorthPointOut } from "../api";
import { formatMoney } from "../lib/format";

echarts.use([LineChart, GridComponent, TooltipComponent, CanvasRenderer]);

type Props = {
  points: NetWorthPointOut[];
  currency: string;
};

export function NetWorthChart({ points, currency }: Props) {
  const container = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!container.current) return;
    const chart = echarts.init(container.current);
    chart.setOption({
      grid: { left: 16, right: 16, top: 16, bottom: 16, containLabel: true },
      tooltip: {
        trigger: "axis",
        // Show the exact string from the API rather than the plotted approximation.
        formatter: (params: unknown) => {
          const [first] = params as { dataIndex: number }[];
          const point = first ? points[first.dataIndex] : undefined;
          return point ? `${point.day}<br/>${formatMoney(point.value, currency)}` : "";
        },
      },
      xAxis: { type: "category", data: points.map((p) => p.day), boundaryGap: false },
      yAxis: { type: "value", scale: true },
      series: [
        {
          type: "line",
          // The only place amounts become numbers: ECharts needs them to plot.
          data: points.map((p) => Number(p.value)),
          showSymbol: false,
          areaStyle: { opacity: 0.08 },
        },
      ],
    });
    const observer = new ResizeObserver(() => chart.resize());
    observer.observe(container.current);
    return () => {
      observer.disconnect();
      chart.dispose();
    };
  }, [points, currency]);

  return <div ref={container} className="h-72 w-full" />;
}
