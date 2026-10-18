// One formatter per precision: catalogs format two prices per row, and each `toLocaleString` call with options builds
// a new `Intl.NumberFormat`.
const formatters = new Map<number, Intl.NumberFormat>();

export function formatModelPrice(value: number | null, suffix = ""): string {
  if (value === null) return "—";
  if (value === 0) return `free${suffix}`;
  const digits = value < 0.01 ? 4 : value < 1 ? 3 : value < 100 ? 2 : 0;
  let formatter = formatters.get(digits);
  if (!formatter) { formatter = new Intl.NumberFormat(undefined, { minimumFractionDigits: 0, maximumFractionDigits: digits }); formatters.set(digits, formatter); }
  return `$${formatter.format(value)}${suffix}`;
}
