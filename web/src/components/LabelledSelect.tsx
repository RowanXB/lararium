import { useId } from "react";

import { Label } from "./ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "./ui/select";

type Props<T extends string> = {
  label: string;
  value: T | undefined;
  options: readonly { value: T; label: string }[];
  onChange: (value: T) => void;
  placeholder?: string;
};

export function LabelledSelect<T extends string>({
  label,
  value,
  options,
  onChange,
  placeholder,
}: Props<T>) {
  const id = useId();
  return (
    <div className="grid gap-1.5">
      <Label htmlFor={id}>{label}</Label>
      <Select value={value ?? ""} onValueChange={(next) => onChange(next as T)}>
        <SelectTrigger id={id} className="w-full min-w-36">
          <SelectValue placeholder={placeholder} />
        </SelectTrigger>
        <SelectContent>
          {options.map((option) => (
            <SelectItem key={option.value} value={option.value}>
              {option.label}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </div>
  );
}
