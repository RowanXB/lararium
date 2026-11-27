import { useEffect, useId, useState } from "react";

import { Input } from "./ui/input";

// With nothing in use yet, these show the owner what a category looks like.
const SUGGESTIONS = ["salary", "family"];

type Props = {
  value: string | null;
  known: readonly string[];
  onChange: (category: string | null) => void;
  label?: string;
};

// A combobox over the categories in use that also takes a new one typed in. The change is
// reported when the field is left or Enter is pressed, not on every key, so a write is
// never made for half a word. The server normalises what is typed.
export function CategoryPicker({ value, known, onChange, label = "Category" }: Props) {
  const listId = useId();
  const [draft, setDraft] = useState(value ?? "");
  useEffect(() => setDraft(value ?? ""), [value]);

  function commit() {
    const next = draft.trim() === "" ? null : draft.trim();
    if (next !== value) onChange(next);
  }

  return (
    <>
      <Input
        list={listId}
        aria-label={label}
        placeholder="Choose"
        className="h-8 w-36"
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={commit}
        onKeyDown={(e) => {
          if (e.key === "Enter") commit();
        }}
      />
      <datalist id={listId}>
        {(known.length > 0 ? known : SUGGESTIONS).map((category) => (
          <option key={category} value={category} />
        ))}
      </datalist>
    </>
  );
}
