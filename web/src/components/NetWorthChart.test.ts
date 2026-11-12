import { expect, it } from "vitest";

import { buildChartOption } from "./NetWorthChart";

const points = [
  { day: "2026-01-01", value: "900.00", missing_count: 1 },
  { day: "2026-01-02", value: "900.00", missing_count: 1 },
  { day: "2026-01-03", value: "1500.00", missing_count: 0 },
];

it("marks the days that leave out a missing price or FX rate", () => {
  const option = buildChartOption(points, "GBP");
  const [, incomplete] = option.series;
  expect(incomplete.data).toEqual([900, 900, null]);
});

it("says in the tooltip when a day is incomplete", () => {
  const option = buildChartOption(points, "GBP");
  expect(option.tooltip.formatter([{ dataIndex: 0 }])).toBe(
    "2026-01-01<br/>£900.00<br/>1 item is missing a price or FX rate and is excluded",
  );
  expect(option.tooltip.formatter([{ dataIndex: 2 }])).toBe("2026-01-03<br/>£1,500.00");
});
