import { useState } from "react";
import { formatPrice, parsePrice } from "./price";

export function PriceInput({ label, placeholder, value, onValueChange }: {
  label: string; placeholder: string; value: number | "";
  onValueChange: (value: number | "") => void;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  return <input required aria-label={label} placeholder={placeholder} type="text"
    inputMode="numeric" value={editing ? draft : value === "" ? "" : formatPrice(value)}
    onFocus={() => { setDraft(String(value)); setEditing(true); }}
    onChange={(event) => {
      const parsed = parsePrice(event.target.value);
      event.target.setCustomValidity(parsed === null || parsed === 0 ? "Введите целую положительную цену" : "");
      setDraft(event.target.value);
      onValueChange(parsed ?? "");
    }}
    onBlur={() => setEditing(false)} />;
}
