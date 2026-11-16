/**
 * it-IT formatting of API values. Decimals arrive as strings and are formatted with
 * string/BigInt arithmetic only: no float conversion of displayed values.
 */

const DECIMAL_RE = /^([+-]?)(\d*)(?:\.(\d*))?$/;

interface Decimal {
  negative: boolean;
  intDigits: string;
  fracDigits: string;
}

function parseDecimal(value: string): Decimal | null {
  const m = DECIMAL_RE.exec(value.trim());
  if (!m || (m[2] === "" && (m[3] ?? "") === "")) return null;
  return { negative: m[1] === "-", intDigits: m[2] || "0", fracDigits: m[3] ?? "" };
}

/** Round HALF_UP (away from zero on .5) to `places` decimals. */
function roundHalfUp(d: Decimal, places: number): Decimal {
  const frac = d.fracDigits.padEnd(places + 1, "0");
  let scaled = BigInt(d.intDigits + frac.slice(0, places));
  if (frac.charCodeAt(places) >= "5".charCodeAt(0)) scaled += 1n;
  const digits = scaled.toString().padStart(places + 1, "0");
  const cut = digits.length - places;
  return {
    // no negative zero: a value that rounds to 0 is shown unsigned
    negative: d.negative && scaled !== 0n,
    intDigits: digits.slice(0, cut),
    fracDigits: digits.slice(cut),
  };
}

function groupThousands(intDigits: string): string {
  return intDigits.replace(/\B(?=(\d{3})+(?!\d))/g, ".");
}

function render(d: Decimal): string {
  const int = groupThousands(d.intDigits.replace(/^0+(?=\d)/, ""));
  return (d.negative ? "-" : "") + int + (d.fracDigits ? "," + d.fracDigits : "");
}

/** Fixed `places` decimals, HALF_UP, it-IT separators ("1.234,50"). */
export function formatDecimal(value: string, places: number): string {
  const d = parseDecimal(value);
  return d ? render(roundHalfUp(d, places)) : value;
}

const MINOR_UNITS: Record<string, number> = { JPY: 0 };
const SYMBOLS: Record<string, string> = { EUR: "€", USD: "$", GBP: "£", JPY: "¥" };

/** "1234.5","EUR" → "1.234,50 €"; JPY without decimals; never "-0,00". */
export function formatMoney(value: string, currency: string): string {
  const d = parseDecimal(value);
  if (!d) return value;
  const places = MINOR_UNITS[currency] ?? 2;
  return `${render(roundHalfUp(d, places))} ${SYMBOLS[currency] ?? currency}`;
}

/** P&L: formatMoney with "+" on gains ("+12,50 €", "-3,00 €", "0,00 €"). */
export function formatSignedMoney(value: string, currency: string): string {
  const text = formatMoney(value, currency);
  return parseDecimal(value) && !text.startsWith("-") && /[1-9]/.test(text) ? `+${text}` : text;
}

/** The colour of a signed figure by its displayed (rounded) value: "gain" at or above
 * zero, "loss" below, null when it is not a number. */
export function signTone(value: string | null | undefined, places = 2): "gain" | "loss" | null {
  const d = value == null ? null : parseDecimal(value);
  if (!d) return null;
  return roundHalfUp(d, places).negative ? "loss" : "gain";
}

/** Quantities: up to 8 decimals (HALF_UP), trailing zeros removed. */
export function formatQty(value: string): string {
  const d = parseDecimal(value);
  if (!d) return value;
  const r = roundHalfUp(d, 8);
  return render({ ...r, fracDigits: r.fracDigits.replace(/0+$/, "") });
}

/** Percentage points: "1.25" → "+1,25%", "-0.5" → "-0,50%", "0" → "0,00%". */
export function formatPct(value: string, places = 2): string {
  const d = parseDecimal(value);
  if (!d) return value;
  const r = roundHalfUp(d, places);
  const isZero = /^0*$/.test(r.intDigits + r.fracDigits);
  return `${!r.negative && !isZero ? "+" : ""}${render(r)}%`;
}

const DATE_TIME = new Intl.DateTimeFormat("it-IT", {
  timeZone: "Europe/Rome",
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hourCycle: "h23",
});

/** ISO timestamp → "dd/MM/yyyy HH:mm:ss" in Europe/Rome. */
export function formatDateTime(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  const p = Object.fromEntries(DATE_TIME.formatToParts(date).map((x) => [x.type, x.value]));
  return `${p.day}/${p.month}/${p.year} ${p.hour}:${p.minute}:${p.second}`;
}

/** Bytes as "512 B", "1,5 KB", "2,3 MB" (one decimal, it-IT). */
export function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${formatQty((Math.round((n / 1024) * 10) / 10).toString())} KB`;
  return `${formatQty((Math.round((n / 1024 / 1024) * 10) / 10).toString())} MB`;
}
