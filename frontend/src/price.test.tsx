import { useState } from "react";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it } from "vitest";
import { PriceInput } from "./PriceInput";
import { formatPrice, formatRubles, parsePrice } from "./price";

afterEach(cleanup);

it("formats integer RUB deterministically with dots and preserves arithmetic", () => {
  expect([1000, 10000, 100000, 1000000, 10000000].map(formatPrice)).toEqual([
    "1.000", "10.000", "100.000", "1.000.000", "10.000.000",
  ]);
  expect(formatRubles(-10000)).toBe("-10.000 ₽");
  expect(parsePrice("10.000.000")).toBe(10000000);
  expect(parsePrice("10 000 000")).toBe(10000000);
  expect(parsePrice("")).toBe("");
  expect(parsePrice("1,5")).toBeNull();
  expect(parsePrice("1.5")).toBeNull();
  expect(parsePrice("1e6")).toBeNull();
  expect(Number(parsePrice("1.000.000")) * 0.07).toBe(70000);
});

it("groups each digit before blur while keeping the parent value numeric", async () => {
  function Harness() {
    const [value, setValue] = useState<number | "">("");
    return <><PriceInput label="Цена" placeholder="Цена" value={value} onValueChange={setValue} />
      <output>{typeof value}:{value}</output></>;
  }
  const user = userEvent.setup();
  render(<Harness />);
  const input = screen.getByLabelText("Цена") as HTMLInputElement;
  await user.click(input);
  for (const [digit, expected] of [
    ["3", "3"], ["0", "30"], ["0", "300"], ["0", "3.000"],
    ["0", "30.000"], ["0", "300.000"], ["0", "3.000.000"],
    ["0", "30.000.000"],
  ]) {
    await user.keyboard(digit);
    expect(input).toHaveFocus();
    expect(input).toHaveValue(expected);
  }
  expect(screen.getByText("number:30000000")).toBeInTheDocument();
  await user.keyboard("{Backspace}");
  expect(input).toHaveValue("3.000.000");
  expect(input).toHaveFocus();
  await user.clear(input);
  expect(input).toHaveValue("");
  expect(screen.getByText("string:")).toBeInTheDocument();
  for (const pasted of ["4100000", "4.100.000", "4 100 000"]) {
    await user.paste(pasted);
    expect(input).toHaveValue("4.100.000");
    expect(input).toHaveFocus();
    expect(screen.getByText("number:4100000")).toBeInTheDocument();
    await user.clear(input);
  }
});

it("keeps the caret by digit when editing or deleting within grouped text", async () => {
  function Harness() {
    const [value, setValue] = useState<number | "">(4100000);
    return <><PriceInput label="Цена" placeholder="Цена" value={value} onValueChange={setValue} />
      <output>{typeof value}:{value}</output></>;
  }
  const user = userEvent.setup();
  render(<Harness />);
  const input = screen.getByLabelText("Цена") as HTMLInputElement;
  await user.click(input);
  input.setSelectionRange(2, 3);
  await user.keyboard("2");
  expect(input).toHaveValue("4.200.000");
  expect(input.selectionStart).toBe(3);
  expect(screen.getByText("number:4200000")).toBeInTheDocument();

  input.setSelectionRange(3, 3);
  await user.keyboard("{Delete}");
  expect(input).toHaveValue("420.000");
  expect(input).toHaveFocus();
  input.setSelectionRange(4, 4);
  await user.keyboard("{Backspace}");
  expect(input).toHaveValue("42.000");
  expect(screen.getByText("number:42000")).toBeInTheDocument();

  await user.keyboard("x");
  expect(input).toHaveValue("42.000");
  expect(screen.getByText("number:42000")).toBeInTheDocument();

  input.setSelectionRange(2, 2);
  await user.keyboard("{Delete}");
  expect(input).toHaveValue("4.200");
  expect(screen.getByText("number:4200")).toBeInTheDocument();
});
