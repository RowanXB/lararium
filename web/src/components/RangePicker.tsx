import { useId, useState } from "react";

import { todayForInput } from "../lib/dates";
import { isValidRange, PRESETS, type PresetKey, presetRange, type Range } from "../lib/range";
import { Input } from "./ui/input";
import { Label } from "./ui/label";

export type RangeChoice = { preset: PresetKey | "custom"; range: Range };

export function initialChoice(preset: PresetKey = "90d"): RangeChoice {
  return { preset, range: presetRange(preset) };
}

// The dates a link carries to the page it opens, as search params.
export type RangeSearch = { preset?: PresetKey | "custom"; start?: string; end?: string };

export function rangeSearch(choice: RangeChoice): RangeSearch {
  return { preset: choice.preset, ...choice.range };
}

// A route's validateSearch: what the URL says, keeping only what can be a range.
export function readRangeSearch(search: Record<string, unknown>): RangeSearch {
  const text = (value: unknown) => (typeof value === "string" ? value : undefined);
  const preset = PRESETS.find((p) => p.key === search.preset)?.key;
  return {
    preset: search.preset === "custom" ? "custom" : preset,
    start: text(search.start),
    end: text(search.end),
  };
}

// A link opens on the dates it was made with: a preset whose dates were another day's
// ("This month" linked on 30 November, opened on 1 December) opens them as custom, one
// without dates or with today's as the preset; a custom range only when it is one. Anything
// else opens on the default.
export function choiceFrom(search: RangeSearch): RangeChoice {
  const range = { start: search.start ?? "", end: search.end ?? "" };
  if (search.preset && search.preset !== "custom") {
    const today = presetRange(search.preset);
    const moved = range.start !== today.start || range.end !== today.end;
    if (moved && isValidRange(range)) return { preset: "custom", range };
    return initialChoice(search.preset);
  }
  if (search.preset === "custom" && isValidRange(range)) return { preset: "custom", range };
  return initialChoice();
}

type Props = { choice: RangeChoice; onChange: (choice: RangeChoice) => void };

const chip =
  "rounded-md px-3 py-1.5 text-sm text-muted-foreground transition-colors hover:text-foreground aria-pressed:bg-primary aria-pressed:font-medium aria-pressed:text-primary-foreground";

// The dates everything on the page covers: a preset, or any two days.
export function RangePicker({ choice, onChange }: Props) {
  const fromId = useId();
  const toId = useId();
  // What is typed, which may not be a range yet; the page keeps the last one that was.
  const [draft, setDraft] = useState<Range>(choice.range);
  const today = todayForInput();
  const custom = choice.preset === "custom";

  function edit(next: Range) {
    setDraft(next);
    if (isValidRange(next, today)) onChange({ preset: "custom", range: next });
  }

  return (
    <div className="flex flex-wrap items-center gap-3">
      <div role="group" aria-label="Dates" className="flex flex-wrap gap-1 rounded-lg border bg-card p-1">
        {PRESETS.map((preset) => (
          <button
            key={preset.key}
            type="button"
            aria-pressed={choice.preset === preset.key}
            className={chip}
            onClick={() => onChange({ preset: preset.key, range: presetRange(preset.key) })}
          >
            {preset.label}
          </button>
        ))}
        <button
          type="button"
          aria-pressed={custom}
          className={chip}
          onClick={() => {
            setDraft(choice.range);
            onChange({ preset: "custom", range: choice.range });
          }}
        >
          Custom
        </button>
      </div>
      {custom && (
        <div className="flex flex-wrap items-center gap-2">
          <Label htmlFor={fromId}>From</Label>
          <Input
            id={fromId}
            type="date"
            className="w-auto"
            max={draft.end || today}
            value={draft.start}
            onChange={(e) => edit({ ...draft, start: e.target.value })}
          />
          <Label htmlFor={toId}>to</Label>
          <Input
            id={toId}
            type="date"
            className="w-auto"
            min={draft.start}
            max={today}
            value={draft.end}
            onChange={(e) => edit({ ...draft, end: e.target.value })}
          />
          {!isValidRange(draft, today) && (
            <p className="text-sm text-warning">Pick a start on or before the end, up to today</p>
          )}
        </div>
      )}
    </div>
  );
}
