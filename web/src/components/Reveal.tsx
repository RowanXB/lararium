import { type ReactNode, useState } from "react";

// Content that slides open and shut: the row grows from 0 to its content's height, so the
// motion follows whatever is inside. Mounted on first open and kept, so it can slide shut.
export function Reveal({ open, id, children }: { open: boolean; id: string; children: ReactNode }) {
  const [opened, setOpened] = useState(open);
  if (open && !opened) setOpened(true);
  return (
    <div
      id={id}
      data-open={open}
      aria-hidden={!open}
      inert={!open}
      className="grid grid-rows-[0fr] opacity-0 transition-[grid-template-rows,opacity] duration-300 ease-out data-[open=true]:grid-rows-[1fr] data-[open=true]:opacity-100 motion-reduce:transition-none"
    >
      {/* The padding keeps focus rings inside the clipped box. */}
      <div className="min-h-0 overflow-hidden">
        <div className="p-1 pt-3">{opened && children}</div>
      </div>
    </div>
  );
}
