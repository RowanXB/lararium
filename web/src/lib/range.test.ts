import { expect, it } from "vitest";

import { bucketFor, dayLabel, isValidRange, periodLabel, presetRange } from "./range";

const NOW = new Date(2026, 10, 26); // 26 November 2026, local time

it("gives each preset's range", () => {
  expect(presetRange("month", NOW)).toEqual({ start: "2026-11-01", end: "2026-11-26" });
  expect(presetRange("last-month", NOW)).toEqual({ start: "2026-10-01", end: "2026-10-31" });
  expect(presetRange("90d", NOW)).toEqual({ start: "2026-08-29", end: "2026-11-26" });
  expect(presetRange("ytd", NOW)).toEqual({ start: "2026-01-01", end: "2026-11-26" });
  expect(presetRange("1y", NOW)).toEqual({ start: "2025-11-27", end: "2026-11-26" });
});

it("picks bars that fit the range", () => {
  expect(bucketFor({ start: "2026-11-01", end: "2026-11-26" })).toBe("day");
  expect(bucketFor({ start: "2026-08-29", end: "2026-11-26" })).toBe("week");
  expect(bucketFor({ start: "2025-11-27", end: "2026-11-26" })).toBe("month");
});

it("refuses a range that is incomplete, backwards or in the future", () => {
  const today = "2026-11-26";
  expect(isValidRange({ start: "2026-11-01", end: "2026-11-26" }, today)).toBe(true);
  expect(isValidRange({ start: "", end: "2026-11-26" }, today)).toBe(false);
  expect(isValidRange({ start: "2026-11-02", end: "2026-11-01" }, today)).toBe(false);
  expect(isValidRange({ start: "2026-11-01", end: "2026-11-27" }, today)).toBe(false);
});

it("labels a period by its bucket", () => {
  expect(periodLabel("2026-11-03", "day")).toBe("3 Nov");
  expect(periodLabel("2026-11-01", "month")).toBe("Nov 2026");
  expect(dayLabel("2026-11-03")).toBe("3 Nov 2026");
});
