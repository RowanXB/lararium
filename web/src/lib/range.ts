import { todayForInput } from "./dates";

export type Range = { start: string; end: string };
export type PresetKey = "month" | "last-month" | "90d" | "ytd" | "1y";
export type Bucket = "day" | "week" | "month";

const pad = (n: number) => String(n).padStart(2, "0");
const iso = (d: Date) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;

// Each preset's range ending today (last month: ending on its last day), in local time.
export const PRESETS: { key: PresetKey; label: string; range: (now: Date) => Range }[] = [
  {
    key: "month",
    label: "This month",
    range: (now) => ({ start: iso(new Date(now.getFullYear(), now.getMonth(), 1)), end: iso(now) }),
  },
  {
    key: "last-month",
    label: "Last month",
    range: (now) => ({
      start: iso(new Date(now.getFullYear(), now.getMonth() - 1, 1)),
      end: iso(new Date(now.getFullYear(), now.getMonth(), 0)),
    }),
  },
  {
    key: "90d",
    label: "90 days",
    range: (now) => ({
      start: iso(new Date(now.getFullYear(), now.getMonth(), now.getDate() - 89)),
      end: iso(now),
    }),
  },
  {
    key: "ytd",
    label: "This year",
    range: (now) => ({ start: iso(new Date(now.getFullYear(), 0, 1)), end: iso(now) }),
  },
  {
    key: "1y",
    label: "12 months",
    range: (now) => ({
      start: iso(new Date(now.getFullYear() - 1, now.getMonth(), now.getDate() + 1)),
      end: iso(now),
    }),
  },
];

export function presetRange(key: PresetKey, now = new Date()): Range {
  return PRESETS.find((preset) => preset.key === key)!.range(now);
}

function daysBetween(range: Range): number {
  return Math.round((Date.parse(range.end) - Date.parse(range.start)) / 86_400_000) + 1;
}

// Bars a chart can show without crowding: days up to about six weeks, weeks up to half a year.
export function bucketFor(range: Range): Bucket {
  const days = daysBetween(range);
  if (days <= 45) return "day";
  if (days <= 190) return "week";
  return "month";
}

// A range the API takes: both days given, in order, and no later than today.
export function isValidRange(range: Range, today = todayForInput()): boolean {
  const day = /^\d{4}-\d{2}-\d{2}$/;
  return day.test(range.start) && day.test(range.end) && range.start <= range.end && range.end <= today;
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

// "2026-11-03" → "3 Nov" (a day or the week starting on it) or "Nov 2026" (a month).
export function periodLabel(start: string, bucket: Bucket): string {
  const [year, month, day] = start.split("-").map(Number);
  return bucket === "month" ? `${MONTHS[month - 1]} ${year}` : `${day} ${MONTHS[month - 1]}`;
}

// "2026-11-03" → "3 Nov 2026".
export function dayLabel(day: string): string {
  const [year, month, date] = day.split("-").map(Number);
  return `${date} ${MONTHS[month - 1]} ${year}`;
}
