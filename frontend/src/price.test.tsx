import { useState } from "react";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
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

it("keeps ordinary cursor editing, deletion, replacement, paste and empty input usable", async () => {
  function Harness() {
    const [value, setValue] = useState<number | "">(10000000);
    return <><PriceInput label="Цена" placeholder="Цена" value={value} onValueChange={setValue} />
      <output>{typeof value}:{value}</output></>;
  }
  const user = userEvent.setup();
  render(<Harness />);
  const input = screen.getByLabelText("Цена");
  expect(input).toHaveValue("10.000.000");
  await user.click(input);
  expect(input).toHaveValue("10000000");
  await user.keyboard("{End}{Backspace}");
  expect(input).toHaveValue("1000000");
  await user.clear(input);
  expect(input).toHaveValue("");
  await user.paste("12.345.000");
  expect(screen.getByText("number:12345000")).toBeInTheDocument();
  fireEvent.blur(input);
  expect(input).toHaveValue("12.345.000");
});
