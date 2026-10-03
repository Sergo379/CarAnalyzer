/** Integer RUB presentation only. API/form state stays numeric. */
export function formatPrice(value: number): string {
  if (!Number.isFinite(value)) return "—";
  return Math.trunc(value).toString().replace(/\B(?=(\d{3})+(?!\d))/g, ".");
}

export function formatRubles(value: number): string {
  return `${formatPrice(value)} ₽`;
}

export function parsePrice(value: string): number | "" | null {
  if (!value.trim()) return "";
  if (!/^[\d.\s]+$/.test(value)) return null;
  if (value.includes(".") && !/^\d{1,3}(?:\.\d{3})+$/.test(value.trim())) return null;
  const digits = value.replace(/[.\s]/g, "");
  if (!digits) return "";
  const number = Number(digits);
  return Number.isSafeInteger(number) ? number : null;
}
