import { useEffect, useId, useRef, useState } from "react";

import { Input } from "./ui/input";

// With nothing in use yet, these show the owner what a category looks like.
const SUGGESTIONS = ["salary", "family"];

type Props = {
  value: string | null;
  known: readonly string[];
  // Shown while nothing is in use yet; the Income page's by default.
  suggestions?: readonly string[];
  // A write's promise, when there is one: the picker sends the same value again after it
  // fails, and only once while it is in flight or after it succeeded.
  onChange: (category: string) => void | Promise<unknown>;
  label?: string;
};

// A combobox over the categories in use that also takes a new one typed in. The change is
// reported when the field is left or Enter is pressed, not on every key, so a write is
// never made for half a word. The server normalises what is typed.
export function CategoryPicker({
  value,
  known,
  suggestions = SUGGESTIONS,
  onChange,
  label = "Category",
}: Props) {
  const listId = useId();
  const [draft, setDraft] = useState(value ?? "");
  // What was last sent, so Enter followed by a blur before the refetch lands sends once. A
  // failed write forgets it, so the owner can try the same category again.
  const sent = useRef<string | null>(null);
  useEffect(() => {
    setDraft(value ?? "");
    sent.current = null;
  }, [value]);

  // An empty or unchanged field sends nothing: clearing is an explicit button, never a
  // side effect of leaving the field.
  function commit() {
    const next = draft.trim();
    if (next === "" || next === value || next === sent.current) {
      setDraft(value ?? "");
      return;
    }
    sent.current = next;
    onChange(next)?.catch(() => {
      if (sent.current === next) sent.current = null;
    });
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
        {(known.length > 0 ? known : suggestions).map((category) => (
          <option key={category} value={category} />
        ))}
      </datalist>
    </>
  );
}
