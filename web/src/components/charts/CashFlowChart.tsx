import { useMemo } from "react";

import type { CashFlowOut } from "../../api";
import { formatMoney, formatMoneyShort } from "../../lib/format";
import { periodLabel } from "../../lib/range";
import { AXIS, CHART, TOOLTIP } from "../../lib/theme";
import { EChart } from "./EChart";

const BUCKET_WORDS = { day: "day", week: "week starting", month: "month" } as const;

export function buildCashFlowOption(flow: CashFlowOut, currency: string) {
  const bar = (name: string, color: string, values: string[]) => ({
    type: "bar",
    name,
    // Amounts become numbers only to be plotted; the tooltip shows the exact strings.
    data: values.map(Number),
    itemStyle: { color, borderRadius: [3, 3, 0, 0] },
    barMaxWidth: 18,
    barGap: "20%",
  });
  return {
    grid: { left: 8, right: 16, top: 16, bottom: 8, containLabel: true },
    tooltip: {
      ...TOOLTIP,
      trigger: "axis",
      axisPointer: { type: "shadow", shadowStyle: { color: "rgba(122, 162, 255, 0.08)" } },
      formatter: (params: unknown) => {
        const [first] = params as { dataIndex: number }[];
        const period = first ? flow.periods[first.dataIndex] : undefined;
        if (!period) return "";
        return [
          periodLabel(period.start, flow.bucket),
          `Income ${formatMoney(period.income, currency)}`,
          `Spending ${formatMoney(period.spending, currency)}`,
        ].join("<br/>");
      },
    },
    xAxis: {
      ...AXIS,
      type: "category",
      data: flow.periods.map((p) => periodLabel(p.start, flow.bucket)),
      splitLine: { show: false },
    },
    yAxis: {
      ...AXIS,
      type: "value",
      axisLabel: {
        ...AXIS.axisLabel,
        formatter: (value: number) => formatMoneyShort(value, currency),
      },
    },
    series: [
      bar(
        "Income",
        CHART.income,
        flow.periods.map((p) => p.income),
      ),
      bar(
        "Spending",
        CHART.spending,
        flow.periods.map((p) => p.spending),
      ),
    ],
  };
}

export function CashFlowChart({ flow, currency }: { flow: CashFlowOut; currency: string }) {
  const option = useMemo(() => buildCashFlowOption(flow, currency), [flow, currency]);
  return (
    <EChart
      option={option}
      label={`Income and spending by ${BUCKET_WORDS[flow.bucket]}`}
      className="h-64 w-full"
    />
  );
}
