/** Pure helpers of «Mercati v2» (plan 30): countdowns, sparklines, big numbers, the
 * calendar's days. */
import type { CalendarEvent, OverviewItem } from "@/api/marketsV2Types";
import { formatDecimal } from "@/lib/format";

/** "2 h 05 min", "3 g 4 h", "12 min", "meno di 1 min" until `iso` from `now`. */
export function countdown(iso: string, now: Date): string {
  const ms = new Date(iso).getTime() - now.getTime();
  if (!Number.isFinite(ms)) return "";
  const min = Math.floor(ms / 60_000);
  if (min < 1) return "meno di 1 min";
  const d = Math.floor(min / 1440);
  const h = Math.floor((min % 1440) / 60);
  const m = min % 60;
  if (d > 0) return h > 0 ? `${d} g ${h} h` : `${d} g`;
  if (h > 0) return `${h} h ${String(m).padStart(2, "0")} min`;
  return `${m} min`;
}

/** «Aperto · chiude tra …» / «Chiuso · apre tra …» of an overview item. */
export function marketLine(it: Pick<OverviewItem, "is_open" | "next_open" | "next_close" | "market">, now: Date): string {
  if (it.is_open == null) return "";
  if (it.is_open) {
    if (it.market === "CRYPTO" || !it.next_close) return "Aperto";
    return `Aperto · chiude tra ${countdown(it.next_close, now)}`;
  }
  return it.next_open ? `Chiuso · apre tra ${countdown(it.next_open, now)}` : "Chiuso";
}

/** An SVG polyline `points` attribute of the values in a w×h box (2 px padding). */
export function sparkPoints(points: [number, number][], w: number, h: number): string {
  if (points.length < 2) return "";
  const ts = points.map((p) => p[0]);
  const vs = points.map((p) => p[1]);
  const t0 = Math.min(...ts);
  const t1 = Math.max(...ts);
  const lo = Math.min(...vs);
  const hi = Math.max(...vs);
  const x = (t: number) => (t1 === t0 ? 0 : ((t - t0) / (t1 - t0)) * w);
  const y = (v: number) => (hi === lo ? h / 2 : 2 + (1 - (v - lo) / (hi - lo)) * (h - 4));
  return points.map(([t, v]) => `${x(t).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
}

/** The item's value with its decimals ("6.910,39", "4,912%"). */
export function overviewValue(it: Pick<OverviewItem, "value" | "decimals" | "unit">): string {
  if (it.value == null) return "—";
  return formatDecimal(it.value, it.decimals) + (it.unit === "%" ? "%" : "");
}

/** Market cap as "4,98 T", "68,57 Mld", "512,3 Mln". */
export function bigNumber(value: string | null | undefined): string {
  if (!value) return "—";
  const n = Number(value);
  if (!Number.isFinite(n)) return "—";
  const units: [number, string][] = [
    [1e12, "T"],
    [1e9, "Mld"],
    [1e6, "Mln"],
  ];
  for (const [u, s] of units) if (Math.abs(n) >= u) return `${formatDecimal((n / u).toFixed(2), 2)} ${s}`;
  return formatDecimal(n.toFixed(0), 0);
}

/** "2026-11-24" → "mar 24/11". */
export function dayLabel(iso: string): string {
  const [y, m, d] = iso.split("-").map(Number);
  const wd = ["dom", "lun", "mar", "mer", "gio", "ven", "sab"][new Date(Date.UTC(y, m - 1, d)).getUTCDay()];
  return `${wd} ${String(d).padStart(2, "0")}/${String(m).padStart(2, "0")}`;
}

/** The events by day, in date order. */
export function byDay(events: CalendarEvent[]): [string, CalendarEvent[]][] {
  const out = new Map<string, CalendarEvent[]>();
  for (const e of events) out.set(e.date, [...(out.get(e.date) ?? []), e]);
  return [...out.entries()].sort((a, b) => a[0].localeCompare(b[0]));
}
