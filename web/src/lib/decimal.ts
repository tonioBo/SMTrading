/** Exact decimal-string helpers (BigInt): no float on money values. */

const PLAIN_RE = /^([+-]?)(\d+)(?:\.(\d+))?$/;

function toScaled(value: string, places: number): bigint | null {
  const m = PLAIN_RE.exec(value);
  if (!m) return null;
  const n = BigInt(m[2] + (m[3] ?? "").padEnd(places, "0"));
  return m[1] === "-" ? -n : n;
}

function fracLen(value: string): number {
  const dot = value.indexOf(".");
  return dot < 0 ? 0 : value.length - dot - 1;
}

/** a - b as a plain decimal string (trailing zeros stripped); null on invalid input. */
export function subtractDecimal(a: string, b: string): string | null {
  const x0 = a.trim();
  const y0 = b.trim();
  const places = Math.max(fracLen(x0), fracLen(y0));
  const x = toScaled(x0, places);
  const y = toScaled(y0, places);
  if (x === null || y === null) return null;
  const diff = x - y;
  const negative = diff < 0n;
  const digits = (negative ? -diff : diff).toString().padStart(places + 1, "0");
  const int = digits.slice(0, digits.length - places);
  const frac = digits.slice(digits.length - places).replace(/0+$/, "");
  return (negative ? "-" : "") + int + (frac ? "." + frac : "");
}

/** a + b as a plain decimal string (trailing zeros stripped); null on invalid input. */
export function addDecimal(a: string, b: string): string | null {
  const y = b.trim();
  const negated = y.startsWith("-") ? y.slice(1) : y.startsWith("+") ? "-" + y.slice(1) : "-" + y;
  return subtractDecimal(a, negated);
}

/** a × b as a plain decimal string (trailing zeros stripped); null on invalid input. */
export function multiplyDecimal(a: string, b: string): string | null {
  const x0 = a.trim();
  const y0 = b.trim();
  const x = toScaled(x0, fracLen(x0));
  const y = toScaled(y0, fracLen(y0));
  if (x === null || y === null) return null;
  const places = fracLen(x0) + fracLen(y0);
  const product = x * y;
  const negative = product < 0n;
  const digits = (negative ? -product : product).toString().padStart(places + 1, "0");
  const int = digits.slice(0, digits.length - places);
  const frac = digits.slice(digits.length - places).replace(/0+$/, "");
  return (negative && /[1-9]/.test(int + frac) ? "-" : "") + int + (frac ? "." + frac : "");
}

export const AMBIGUOUS_AMOUNT_MESSAGE = "Usa la virgola per i decimali (es. 10.000,00)";
export const INVALID_AMOUNT_MESSAGE = "Importo non valido";

export type ParsedAmount = { ok: true; value: string } | { ok: false; error: string };

// digits with "." thousands separators in groups of 3 ("1.000", "12.345.678")
const GROUPED_INT = /^[1-9]\d{0,2}(\.\d{3})+$/;

/**
 * A non-negative amount typed in it-IT, as a plain decimal string for the API.
 * - With a comma: the comma is the decimal point, dots are thousands separators (groups
 *   of 3): "10.000,50" → "10000.50", "1,5" → "1.5".
 * - Without a comma: "10.000" / "1.000.000" (every dot group of 3 digits) is ambiguous and
 *   refused; otherwise a single dot is the decimal point: "1.5" → "1.5". A leading "0"
 *   ("0.125") cannot be a thousands grouping, so it is a decimal.
 */
export function parseAmount(input: string): ParsedAmount {
  const s = input.trim().replace(/\s/g, "");
  const invalid: ParsedAmount = { ok: false, error: INVALID_AMOUNT_MESSAGE };
  if (s.includes(",")) {
    const parts = s.split(",");
    if (parts.length !== 2) return invalid;
    const [int, frac] = parts;
    if (!/^\d+$/.test(frac)) return invalid;
    if (/^\d+$/.test(int)) return { ok: true, value: `${int}.${frac}` };
    if (GROUPED_INT.test(int)) return { ok: true, value: `${int.replace(/\./g, "")}.${frac}` };
    return invalid;
  }
  if (GROUPED_INT.test(s)) return { ok: false, error: AMBIGUOUS_AMOUNT_MESSAGE };
  return /^\d+(\.\d+)?$/.test(s) ? { ok: true, value: s } : invalid;
}

/** parseAmount's value, or null when the input is not a valid (unambiguous) amount. */
export function normalizeAmount(input: string): string | null {
  const parsed = parseAmount(input);
  return parsed.ok ? parsed.value : null;
}

/** parseAmount's error message, or null when the input is valid. */
export function amountError(input: string): string | null {
  const parsed = parseAmount(input);
  return parsed.ok ? null : parsed.error;
}

/** An API decimal ("1.125") as it-IT input text ("1,125"), no grouping; null → "". */
export function toInputDecimal(apiValue: string | null | undefined): string {
  return apiValue == null ? "" : apiValue.replace(".", ",");
}
