import { formatPrice, parsePrice } from "./price";

function caretAfterDigits(formatted: string, digitsBeforeCaret: number): number {
  if (digitsBeforeCaret === 0) return 0;
  let seen = 0;
  for (let index = 0; index < formatted.length; index += 1) {
    if (/\d/.test(formatted[index]) && ++seen === digitsBeforeCaret) return index + 1;
  }
  return formatted.length;
}

export function PriceInput({ label, placeholder, value, onValueChange }: {
  label: string; placeholder: string; value: number | "";
  onValueChange: (value: number | "") => void;
}) {
  const display = value === "" ? "" : formatPrice(value);

  function applyEdit(input: HTMLInputElement, raw: string, caret: number) {
    // Editing can temporarily break a group (for example, deleting a digit from 3.000).
    // Dots and spaces are separators only; the parent still receives a number.
    const parsed = /^[\d.\s]*$/.test(raw) ? parsePrice(raw.replace(/[.\s]/g, "")) : null;
    if (parsed === null) {
      input.value = display;
      input.setSelectionRange(Math.min(caret, display.length), Math.min(caret, display.length));
      return;
    }

    const formatted = parsed === "" ? "" : formatPrice(parsed);
    const digitsBeforeCaret = raw.slice(0, caret).replace(/\D/g, "").length;
    input.value = formatted;
    input.setCustomValidity(parsed === 0 ? "Введите целую положительную цену" : "");
    const nextCaret = caretAfterDigits(formatted, digitsBeforeCaret);
    input.setSelectionRange(nextCaret, nextCaret);
    onValueChange(parsed);
  }

  return <input required aria-label={label} placeholder={placeholder} type="text"
    inputMode="numeric" value={display}
    onChange={(event) => applyEdit(event.currentTarget, event.currentTarget.value,
      event.currentTarget.selectionStart ?? event.currentTarget.value.length)}
    onKeyDown={(event) => {
      const input = event.currentTarget;
      const start = input.selectionStart;
      if (start === null || start !== input.selectionEnd) return;
      if (event.key === "Backspace" && start > 1 && input.value[start - 1] === ".") {
        event.preventDefault();
        applyEdit(input, input.value.slice(0, start - 2) + input.value.slice(start - 1), start - 2);
      } else if (event.key === "Delete" && input.value[start] === "." && start + 1 < input.value.length) {
        event.preventDefault();
        applyEdit(input, input.value.slice(0, start + 1) + input.value.slice(start + 2), start);
      }
    }} />;
}
