import { LineChart } from "echarts/charts";
import { GridComponent, TooltipComponent } from "echarts/components";
import * as echarts from "echarts/core";
import { CanvasRenderer } from "echarts/renderers";
import { useEffect, useRef } from "react";

import type { NetWorthPointOut } from "../api";
import { formatMoney } from "../lib/format";

echarts.use([LineChart, GridComponent, TooltipComponent, CanvasRenderer]);

const INCOMPLETE_COLOR = "#d97706"; // amber, as the page's missing-price banner

type Props = {
  points: NetWorthPointOut[];
  currency: string;
};

function missingNote(count: number): string {
  return count === 1
    ? "1 item is missing a price or FX rate and is excluded"
    : `${count} items are missing a price or FX rate and are excluded`;
}

// Each point says how many items it left out, so completeness is per day: a holding can be
// unpriced on earlier days only, and then its first price shows up as a jump in the line.
export function buildChartOption(points: NetWorthPointOut[], currency: string) {
  return {
    grid: { left: 16, right: 16, top: 16, bottom: 16, containLabel: true },
    tooltip: {
      trigger: "axis",
      // Show the exact string from the API rather than the plotted approximation.
      formatter: (params: unknown) => {
        const [first] = params as { dataIndex: number }[];
        const point = first ? points[first.dataIndex] : undefined;
        if (!point) return "";
        const lines = [point.day, formatMoney(point.value, currency)];
        if (point.missing_count > 0) lines.push(missingNote(point.missing_count));
        return lines.join("<br/>");
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
      {
        // The incomplete days again, over the main line, so they stand out.
        type: "line",
        data: points.map((p) => (p.missing_count > 0 ? Number(p.value) : null)),
        showSymbol: true,
        symbolSize: 4,
        itemStyle: { color: INCOMPLETE_COLOR },
        lineStyle: { color: INCOMPLETE_COLOR },
      },
    ],
  };
}

export function NetWorthChart({ points, currency }: Props) {
  const container = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!container.current) return;
    const chart = echarts.init(container.current);
    chart.setOption(buildChartOption(points, currency));
    const observer = new ResizeObserver(() => chart.resize());
    observer.observe(container.current);
    return () => {
      observer.disconnect();
      chart.dispose();
    };
  }, [points, currency]);

  return <div ref={container} className="h-72 w-full" />;
}
