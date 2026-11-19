/**
 * Time series of the line/area charts (the «Andamento» of `components/charts/EquityChart`):
 * numbers (`t` = epoch ms, `v` = value); the API decimals become numbers only for drawing
 * and its labels, which keep the it-IT formatting of format.ts.
 */
import { formatDecimal } from "@/lib/format";

export interface ChartPoint {
  t: number;
  v: number;
}

export interface ChartLine {
  key: string;
  label: string;
  color: string;
  points: ChartPoint[];
  /** reference lines (the goals): a labelled price line, left out of the tooltip */
  reference?: boolean;
  /** a short label of the line (the goal's price line, the index) */
  tag?: string;
  /** a thinner solid stroke (the indices compared by the goals) */
  thin?: boolean;
  /** a thicker stroke (the «Totale» of the portfolios) */
  bold?: boolean;
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

/** chartNumber with "+" on positive values ("+2,00", "-1,50", "0,00"). */
export function signedNumber(value: number, places: number): string {
  const text = chartNumber(value, places);
  return (value > 0 && /[1-9]/.test(text) ? "+" : "") + text;
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
