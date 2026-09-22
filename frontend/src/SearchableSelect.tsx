import { useEffect, useId, useMemo, useRef, useState } from "react";

export interface SelectOption {
  value: string;
  label: string;
}

interface Props {
  label: string;
  value: string;
  options: SelectOption[];
  onChange: (value: string) => void;
  allowCustom?: boolean;
  required?: boolean;
}

export function SearchableSelect({
  label,
  value,
  options,
  onChange,
  allowCustom = false,
  required = false,
}: Props) {
  const id = useId();
  const root = useRef<HTMLLabelElement>(null);
  const selected = options.find((option) => option.value === value);
  const [query, setQuery] = useState(selected?.label ?? value);
  const [open, setOpen] = useState(false);
  const [activeIndex, setActiveIndex] = useState(0);

  useEffect(() => {
    setQuery(options.find((option) => option.value === value)?.label ?? value);
  }, [options, value]);

  useEffect(() => {
    function close(event: MouseEvent) {
      if (!root.current?.contains(event.target as Node)) setOpen(false);
    }
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, []);

  const filtered = useMemo(() => {
    const needle = query.trim().toLocaleLowerCase("ru");
    if (!needle || selected?.label === query) return options;
    return options.filter((option) => option.label.toLocaleLowerCase("ru").includes(needle));
  }, [options, query, selected?.label]);

  function choose(option: SelectOption) {
    onChange(option.value);
    setQuery(option.label);
    setOpen(false);
  }

  return (
    <label ref={root} className="combobox-field" htmlFor={id}>
      {label}
      <input
        id={id}
        role="combobox"
        aria-expanded={open}
        aria-controls={`${id}-listbox`}
        aria-autocomplete="list"
        autoComplete="off"
        required={required}
        value={query}
        onFocus={() => { setOpen(true); setActiveIndex(0); }}
        onClick={() => setOpen(true)}
        onChange={(event) => {
          const next = event.target.value;
          setQuery(next);
          setOpen(true);
          setActiveIndex(0);
          if (allowCustom) onChange(next);
        }}
        onKeyDown={(event) => {
          if (event.key === "Escape") {
            setOpen(false);
            return;
          }
          if (event.key === "ArrowDown" || event.key === "ArrowUp") {
            event.preventDefault();
            setOpen(true);
            const direction = event.key === "ArrowDown" ? 1 : -1;
            setActiveIndex((index) =>
              Math.max(0, Math.min(filtered.length - 1, index + direction)),
            );
          }
          if (event.key === "Enter" && open && filtered[activeIndex]) {
            event.preventDefault();
            choose(filtered[activeIndex]);
          }
        }}
      />
      {open && filtered.length > 0 && (
        <div className="combobox-options" role="listbox" id={`${id}-listbox`}>
          {filtered.map((option, index) => (
            <button
              type="button"
              role="option"
              aria-selected={option.value === value}
              className={index === activeIndex ? "active" : ""}
              key={option.value}
              onMouseDown={(event) => event.preventDefault()}
              onClick={() => choose(option)}
            >
              {option.label}
            </button>
          ))}
        </div>
      )}
    </label>
  );
}
