export function formatVND(value: string | number): string {
  const raw = String(value).trim();

  const match = raw.match(/^(-?)(\d+)(?:\.(\d+))?$/);

  if (!match) return `${raw} VND`;

  const [, sign, integer, fraction = ""] = match;

  const grouped = integer.replace(
    /\B(?=(\d{3})+(?!\d))/g,
    "."
  );

  // Preserve fractional precision without floating-point conversion.
  const decimal = fraction.replace(/0+$/, "");

  return `${sign}${grouped}${decimal ? "," + decimal : ""} VND`;
}
