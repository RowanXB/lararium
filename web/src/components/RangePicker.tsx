import { useId, useState } from "react";

import { todayForInput } from "../lib/dates";
import { isValidRange, PRESETS, type PresetKey, presetRange, type Range } from "../lib/range";
import { Input } from "./ui/input";
import { Label } from "./ui/label";

export type RangeChoice = { preset: PresetKey | "custom"; range: Range };

export function initialChoice(preset: PresetKey = "90d"): RangeChoice {
  return { preset, range: presetRange(preset) };
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
