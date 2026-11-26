import { useMemo } from "react";

import type { NetWorthPointOut } from "../../api";
import { formatMoney, formatMoneyShort } from "../../lib/format";
import { periodLabel } from "../../lib/range";
import { AXIS, CHART, TOOLTIP } from "../../lib/theme";
import { EChart } from "./EChart";

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
    grid: { left: 8, right: 16, top: 16, bottom: 8, containLabel: true },
    tooltip: {
      ...TOOLTIP,
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
    xAxis: {
      ...AXIS,
      type: "category",
      data: points.map((p) => p.day),
      boundaryGap: false,
      splitLine: { show: false },
      axisLabel: { ...AXIS.axisLabel, formatter: (day: string) => periodLabel(day, "day") },
    },
    yAxis: {
      ...AXIS,
      type: "value",
      scale: true,
      axisLabel: {
        ...AXIS.axisLabel,
        formatter: (value: number) => formatMoneyShort(value, currency),
      },
    },
    series: [
      {
        type: "line",
        // The only place amounts become numbers: ECharts needs them to plot.
        data: points.map((p) => Number(p.value)),
        showSymbol: false,
        smooth: 0.2,
        lineStyle: { color: CHART.accent, width: 2, shadowColor: CHART.accent, shadowBlur: 12 },
        itemStyle: { color: CHART.accent },
        areaStyle: {
          color: {
            type: "linear",
            x: 0,
            y: 0,
            x2: 0,
            y2: 1,
            colorStops: [
              { offset: 0, color: "rgba(122, 162, 255, 0.28)" },
              { offset: 1, color: "rgba(122, 162, 255, 0)" },
            ],
          },
        },
      },
      {
        // The incomplete days again, over the main line, so they stand out.
        type: "line",
        data: points.map((p) => (p.missing_count > 0 ? Number(p.value) : null)),
        showSymbol: true,
        symbolSize: 4,
        itemStyle: { color: CHART.warning },
        lineStyle: { color: CHART.warning },
      },
    ],
  };
}

export function NetWorthChart({ points, currency }: Props) {
  const option = useMemo(() => buildChartOption(points, currency), [points, currency]);
  return <EChart option={option} label="Net worth over the chosen dates" />;
}
