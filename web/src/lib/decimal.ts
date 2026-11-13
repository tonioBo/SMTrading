/** Exact decimal-string helpers (BigInt): no float on money values. */

const PLAIN_RE = /^([+-]?)(\d+)(?:\.(\d+))?$/;

function toScaled(value: string, places: number): bigint | null {
  const m = PLAIN_RE.exec(value.trim());
  if (!m) return null;
  const n = BigInt(m[2] + (m[3] ?? "").padEnd(places, "0"));
  return m[1] === "-" ? -n : n;
}

function fracLen(value: string): number {
  const dot = value.indexOf(".");
  return dot < 0 ? 0 : value.trim().length - dot - 1;
}

/** a - b as a plain decimal string (trailing zeros stripped); null on invalid input. */
export function subtractDecimal(a: string, b: string): string | null {
  const places = Math.max(fracLen(a), fracLen(b));
  const x = toScaled(a, places);
  const y = toScaled(b, places);
  if (x === null || y === null) return null;
  const diff = x - y;
  const negative = diff < 0n;
  const digits = (negative ? -diff : diff).toString().padStart(places + 1, "0");
  const int = digits.slice(0, digits.length - places);
  const frac = digits.slice(digits.length - places).replace(/0+$/, "");
  return (negative ? "-" : "") + int + (frac ? "." + frac : "");
}

/**
 * A non-negative amount typed by the user ("1.234,56", "1000,5", "1000.50") as a plain
 * decimal string for the API; null when it is not a valid amount. With a comma the dots
 * are thousands separators; without one a single dot is the decimal point.
 */
export function normalizeAmount(input: string): string | null {
  let s = input.trim().replace(/\s/g, "");
  if (s.includes(",")) s = s.replace(/\./g, "").replace(",", ".");
  return /^\d+(\.\d+)?$/.test(s) ? s : null;
}
