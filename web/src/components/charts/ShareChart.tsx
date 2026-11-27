import { format as echartsFormat } from "echarts/core";
import { useMemo } from "react";

import { TOOLTIP } from "../../lib/theme";
import { EChart } from "./EChart";

export type Share = { name: string; value: string; color: string };

// A ring of shares; the amounts and names are listed beside it, so it carries no labels.
export function buildShareOption(shares: Share[], format: (value: string) => string) {
  return {
    tooltip: {
      ...TOOLTIP,
      trigger: "item",
      formatter: (params: unknown) => {
        const share = shares[(params as { dataIndex: number }).dataIndex];
        // The tooltip is HTML, and a name is text from a source (a category): escape it.
        return share ? `${echartsFormat.encodeHTML(share.name)}<br/>${format(share.value)}` : "";
      },
    },
    series: [
      {
        type: "pie",
        radius: ["62%", "88%"],
        padAngle: 2,
        itemStyle: { borderRadius: 4 },
        label: { show: false },
        emphasis: { scaleSize: 4 },
        // Amounts become numbers only to be plotted.
        data: shares.map((s) => ({ name: s.name, value: Number(s.value), itemStyle: { color: s.color } })),
      },
    ],
  };
}

type Props = { shares: Share[]; format: (value: string) => string; label: string };

export function ShareChart({ shares, format, label }: Props) {
  const option = useMemo(() => buildShareOption(shares, format), [shares, format]);
  return <EChart option={option} label={label} className="size-40 shrink-0" />;
}
