// A row of buttons of which one is on: for a choice between a few plain options.
type Props<T extends string> = {
  label: string;
  value: T;
  options: readonly { value: T; label: string }[];
  onChange: (value: T) => void;
};

export function Segmented<T extends string>({ label, value, options, onChange }: Props<T>) {
  return (
    <div className="grid gap-1.5">
      <p className="text-sm font-medium" aria-hidden>
        {label}
      </p>
      <div
        role="group"
        aria-label={label}
        className="flex w-fit flex-wrap gap-1 rounded-lg border bg-card p-1"
      >
      {options.map((option) => (
        <button
          key={option.value}
          type="button"
          aria-pressed={value === option.value}
          className="rounded-md px-3 py-1.5 text-sm text-muted-foreground transition-colors hover:text-foreground aria-pressed:bg-primary aria-pressed:font-medium aria-pressed:text-primary-foreground"
          onClick={() => onChange(option.value)}
        >
          {option.label}
        </button>
      ))}
      </div>
    </div>
  );
}
