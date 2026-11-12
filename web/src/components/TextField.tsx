import { useId } from "react";

import { Input } from "./ui/input";
import { Label } from "./ui/label";

type Props = {
  label: string;
  value: string;
  onChange: (value: string) => void;
  required?: boolean;
  type?: string;
};

export function TextField({ label, value, onChange, required, type }: Props) {
  const id = useId();
  return (
    <div className="grid gap-1.5">
      <Label htmlFor={id}>{label}</Label>
      <Input
        id={id}
        type={type}
        value={value}
        required={required}
        onChange={(e) => onChange(e.target.value)}
      />
    </div>
  );
}
