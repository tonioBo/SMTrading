/**
 * Shared helpers of the canvas charts (`components/charts/`: TradingView Lightweight
 * Charts and the ECharts treemap). A canvas cannot read CSS variables, so the theme
 * tokens of index.css (`--gain: 5 150 105`) are resolved to plain `rgb()` colours here,
 * again whenever the theme changes. Times on the charts are UTC seconds; their labels are
 * Europe/Rome, numbers it-IT (format.ts).
 */
import { chartNumber } from "@/lib/lineChart";

export const TOKENS = ["bg", "fg", "muted", "card", "border", "accent", "gain", "loss", "warn"] as const;
export type Token = (typeof TOKENS)[number];
export type Palette = Record<Token, string>;

/** The dark theme's tokens: the fallback when the page has no computed styles (tests). */
export const FALLBACK: Record<Token, string> = {
  bg: "9 9 11",
  fg: "250 250 250",
  muted: "161 161 170",
  card: "24 24 27",
  border: "39 39 42",
  accent: "16 185 129",
  gain: "16 185 129",
  loss: "244 63 94",
  warn: "245 158 11",
};

/** The current theme's tokens as `rgb(r, g, b)` (read from :root). */
export function readPalette(root: Element | null = typeof document === "undefined" ? null : document.documentElement): Palette {
  const style = root ? getComputedStyle(root) : null;
  const out = {} as Palette;
  for (const t of TOKENS) {
    const raw = style?.getPropertyValue(`--${t}`).trim() || FALLBACK[t];
    out[t] = `rgb(${raw.split(/\s+/).join(", ")})`;
  }
  return out;
}

/**
 * A colour a canvas understands: `rgb(var(--accent))` (the tokens of the lib/ series
 * colours) becomes the palette's value; anything else is returned as it is.
 */
export function cssColor(color: string, palette: Palette): string {
  const m = /^rgb\(var\(--([a-z]+)\)\)$/.exec(color.trim());
  return m && m[1] in palette ? palette[m[1] as Token] : color;
}

/** `color` with an alpha: `rgb(1, 2, 3)` / `#rrggbb` → `rgba(…, a)`. */
export function withAlpha(color: string, alpha: number): string {
  const rgb = /^rgba?\(([^)]+)\)$/.exec(color.trim());
  if (rgb) {
    const [r, g, b] = rgb[1].split(",").map((x) => x.trim());
    return `rgba(${r}, ${g}, ${b}, ${alpha})`;
  }
  const hex = /^#([0-9a-f]{6})$/i.exec(color.trim());
  if (hex) {
    const n = parseInt(hex[1], 16);
    return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${alpha})`;
  }
  return color;
}

/** `[r, g, b]` of an `rgb()` / `#rrggbb` colour (null otherwise). */
export function rgbOf(color: string): [number, number, number] | null {
  const m = /^rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)/.exec(color.trim());
  if (m) return [Number(m[1]), Number(m[2]), Number(m[3])];
  const hex = /^#([0-9a-f]{6})$/i.exec(color.trim());
  if (!hex) return null;
  const n = parseInt(hex[1], 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

/** Epoch milliseconds → the charts' UTC seconds. */
export const toSeconds = (ms: number) => Math.floor(ms / 1000);

const ROME = "Europe/Rome";
const fmt = (o: Intl.DateTimeFormatOptions) => new Intl.DateTimeFormat("it-IT", { timeZone: ROME, ...o });
const TIME = fmt({ hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
const DAY = fmt({ day: "2-digit", month: "2-digit" });
const DAY_LONG = fmt({ day: "2-digit", month: "2-digit", year: "2-digit" });
const MONTH = fmt({ month: "short" });
const YEAR = fmt({ year: "numeric" });

/**
 * Time-axis labels in Europe/Rome (`kind`: lightweight-charts' TickMarkType — 0 year,
 * 1 month, 2 day, 3/4 time): "2026", "nov", "16/11", "09:05".
 */
export function tickLabel(seconds: number, kind: number): string {
  const d = new Date(seconds * 1000);
  if (kind === 0) return YEAR.format(d);
  if (kind === 1) return MONTH.format(d);
  if (kind === 2) return DAY.format(d);
  return TIME.format(d);
}

/** The crosshair's time label: "16/11/26 09:05", or the date only for daily bars. */
export function crosshairLabel(seconds: number, daily: boolean): string {
  const d = new Date(seconds * 1000);
  return daily ? DAY_LONG.format(d) : `${DAY_LONG.format(d)} ${TIME.format(d)}`;
}

/** Decimals for a price: 2, or up to 6 under 1 (crypto, FX). */
export function pricePlaces(v: number): number {
  const a = Math.abs(v);
  return a === 0 || a >= 1 ? 2 : a >= 0.01 ? 4 : 6;
}

/** A price axis label, it-IT: "1.234,50". */
export function priceLabel(v: number): string {
  return chartNumber(v, pricePlaces(v));
}

/** A volume label: "950", "12,3 mila", "4,5 mln". */
export function volumeLabel(v: number): string {
  const a = Math.abs(v);
  if (a >= 1e9) return `${chartNumber(v / 1e9, 1)} mld`;
  if (a >= 1e6) return `${chartNumber(v / 1e6, 1)} mln`;
  if (a >= 1e4) return `${chartNumber(v / 1e3, 1)} mila`;
  return chartNumber(v, 0);
}
