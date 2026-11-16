/**
 * Helpers of the hand-made SVG line chart (`components/LineChart.tsx`). The chart works on
 * numbers (`t` = epoch ms, `v` = value): the API decimals become numbers only for drawing
 * and its labels, which keep the it-IT formatting of format.ts.
 */
import { formatDecimal } from "@/lib/format";

export interface ChartPoint {
  t: number;
  v: number;
}

/** About `count` round ticks (1, 2, 2.5, 5 × 10^n) covering [min, max]. */
export function niceTicks(min: number, max: number, count = 4): number[] {
  if (!Number.isFinite(min) || !Number.isFinite(max)) return [];
  if (min === max) {
    const pad = Math.abs(min) * 0.01 || 1;
    min -= pad;
    max += pad;
  }
  const raw = (max - min) / Math.max(1, count);
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = ([1, 2, 2.5, 5, 10].find((m) => m * mag >= raw) ?? 10) * mag;
  const first = Math.floor(min / step) * step;
  const last = Math.ceil(max / step) * step;
  const out: number[] = [];
  for (let i = 0; first + i * step <= last + step / 1e6; i++) {
    out.push(Number((first + i * step).toPrecision(12)));
  }
  return out;
}

/** The change in percent since the first point; empty without a positive first value. */
export function toPctPoints(points: ChartPoint[]): ChartPoint[] {
  const base = points[0]?.v;
  if (!base || base <= 0) return [];
  return points.map((p) => ({ t: p.t, v: Number(((p.v / base - 1) * 100).toPrecision(12)) }));
}

/** The index of the point closest in time to `t` (points sorted by time), -1 when empty. */
export function nearestIndex(points: ChartPoint[], t: number): number {
  if (points.length === 0) return -1;
  let lo = 0;
  let hi = points.length - 1;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (points[mid].t < t) lo = mid + 1;
    else hi = mid;
  }
  if (lo > 0 && t - points[lo - 1].t <= points[lo].t - t) return lo - 1;
  return lo;
}

/** A chart value with `places` decimals, it-IT separators ("1.234,50"). */
export function chartNumber(value: number, places: number): string {
  return formatDecimal(value.toFixed(places), places);
}

const ROME = "Europe/Rome";
const TIME = new Intl.DateTimeFormat("it-IT", { timeZone: ROME, hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
const DAY = new Intl.DateTimeFormat("it-IT", { timeZone: ROME, day: "2-digit", month: "2-digit" });

/** Axis/tooltip time labels in Europe/Rome: "09:05", "16/11" or "16/11 09:05". */
export function timeLabel(t: number, kind: "time" | "day" | "full"): string {
  const date = new Date(t);
  if (kind === "time") return TIME.format(date);
  if (kind === "day") return DAY.format(date);
  return `${DAY.format(date)} ${TIME.format(date)}`;
}
