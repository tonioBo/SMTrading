/**
 * Data of the candlestick charts (`components/charts/PriceChart`): the API bars become
 * candles, volume bars and moving averages; our executions become markers on the bar
 * they fall in, our stop and target price lines. Pure functions: the chart library is
 * only in the lazily loaded component.
 */
import { priceLabel, toSeconds } from "@/lib/chartTheme";
import { formatMoney } from "@/lib/format";

export interface ApiBar {
  ts: string;
  open?: string;
  high?: string;
  low?: string;
  close: string;
  volume?: string;
}

export interface Candle {
  time: number; // UTC seconds
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface PricePoint {
  time: number;
  value: number;
}

export interface PriceMarker {
  time: number;
  side: "buy" | "sell";
  text: string;
}

export type PriceLineKind = "stop" | "target" | "entry" | "exit";

export interface PriceLine {
  price: number;
  kind: PriceLineKind;
  title: string;
}

/** Candles sorted by time, one per second (the last wins), invalid ones dropped. */
export function toCandles(bars: ApiBar[]): Candle[] {
  const byTime = new Map<number, Candle>();
  for (const b of bars) {
    const t = Date.parse(b.ts);
    const close = Number(b.close);
    if (!Number.isFinite(t) || !Number.isFinite(close)) continue;
    const num = (v: string | undefined) => (v == null || !Number.isFinite(Number(v)) ? close : Number(v));
    const open = num(b.open);
    byTime.set(toSeconds(t), {
      time: toSeconds(t),
      open,
      high: Math.max(num(b.high), open, close),
      low: Math.min(num(b.low), open, close),
      close,
      volume: b.volume == null ? 0 : Number(b.volume) || 0,
    });
  }
  return [...byTime.values()].sort((a, b) => a.time - b.time);
}

/** Bars with a volume: the histogram is drawn only then (FX and some indices have none). */
export const hasVolume = (candles: Candle[]) => candles.some((c) => c.volume > 0);

/** The simple moving average of the closes over `n` bars (from the n-th bar on). */
export function sma(candles: Candle[], n: number): PricePoint[] {
  const out: PricePoint[] = [];
  let sum = 0;
  candles.forEach((c, i) => {
    sum += c.close;
    if (i >= n) sum -= candles[i - n].close;
    if (i >= n - 1) out.push({ time: c.time, value: Number((sum / n).toPrecision(12)) });
  });
  return out;
}

/**
 * The bar a moment falls in: the last bar opened at or before it (a fill before the first
 * bar or more than `maxGap` seconds after the last one has none: it is off the chart).
 */
export function barTimeOf(times: number[], seconds: number, maxGap: number): number | null {
  let lo = 0;
  let hi = times.length - 1;
  let found = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (times[mid] <= seconds) {
      found = mid;
      lo = mid + 1;
    } else hi = mid - 1;
  }
  if (found < 0 || seconds - times[found] > maxGap) return null;
  return times[found];
}

/** A bar's length in seconds, from the timeframe. */
export const barSeconds = (timeframe: "1d" | "1h") => (timeframe === "1d" ? 86_400 : 3_600);

export interface MarkerFill {
  ts: string;
  side: "buy" | "sell";
  qty?: string;
  price?: string;
  text?: string;
}

/**
 * Our executions as markers (▲ buy below the bar, ▼ sell above), on the bar each falls in,
 * sorted by time; fills of one bar and side are merged into one marker («×2»). A fill
 * outside the bars is left out. Weekends and nights: up to 4 bars of gap still count.
 */
export function fillMarkers(fills: MarkerFill[], candles: Candle[], timeframe: "1d" | "1h"): PriceMarker[] {
  const times = candles.map((c) => c.time);
  const merged = new Map<string, PriceMarker & { n: number }>();
  for (const f of fills) {
    const t = Date.parse(f.ts);
    if (!Number.isFinite(t)) continue;
    const at = barTimeOf(times, toSeconds(t), barSeconds(timeframe) * 4);
    if (at == null) continue;
    const key = `${at}-${f.side}`;
    const text = f.text ?? (f.side === "buy" ? "Acquisto" : "Vendita");
    const prev = merged.get(key);
    if (prev) {
      prev.n += 1;
      prev.text = `${text} ×${prev.n}`;
    } else merged.set(key, { time: at, side: f.side, text, n: 1 });
  }
  return [...merged.values()].sort((a, b) => a.time - b.time).map(({ time, side, text }) => ({ time, side, text }));
}

export interface OrderLike {
  role: string | null;
  type: string;
  side: "buy" | "sell";
  limit_price: string | null;
  stop_price: string | null;
  status?: string;
  created_at?: string;
}

/** The price an exit order protects or targets: a stop's trigger, a limit's limit. */
function orderPrice(o: OrderLike): number | null {
  const raw = o.type === "limit" ? o.limit_price : (o.stop_price ?? o.limit_price);
  const v = raw == null ? NaN : Number(raw);
  return Number.isFinite(v) && v > 0 ? v : null;
}

/**
 * The stop and target lines of a set of orders: a `stop_loss` (or a stop-type exit) is
 * «Stop», a `take_profit` (or a limit-type exit) «Target». Of several orders of a kind the
 * newest wins; entry and rejected orders draw nothing. A canceled leg still counts (an
 * OCO target canceled when its stop was filled was the plan).
 */
export function protectionLines(orders: OrderLike[], currencyLabel: (v: number) => string): PriceLine[] {
  const newest = new Map<"stop" | "target", { at: number; price: number }>();
  orders.forEach((o, i) => {
    if (o.role === "entry" || o.role === "liquidation") return;
    const kind = o.role === "stop_loss" ? "stop" : o.role === "take_profit" ? "target" : o.role == null ? (o.type === "limit" ? "target" : o.type.includes("stop") ? "stop" : null) : null;
    if (!kind || o.status === "rejected") return;
    const price = orderPrice(o);
    if (price == null) return;
    const at = o.created_at ? Date.parse(o.created_at) : i;
    const prev = newest.get(kind);
    if (!prev || at >= prev.at) newest.set(kind, { at, price });
  });
  const out: PriceLine[] = [];
  const stop = newest.get("stop");
  const target = newest.get("target");
  if (stop) out.push({ price: stop.price, kind: "stop", title: `Stop ${currencyLabel(stop.price)}` });
  if (target) out.push({ price: target.price, kind: "target", title: `Target ${currencyLabel(target.price)}` });
  return out;
}

export interface ChartWindow {
  timeframe: "1d" | "1h";
  start: Date;
  end: Date;
}

const HOUR = 3_600_000;
const DAY = 24 * HOUR;

/**
 * The window of a trade's chart: hourly bars when it lasted under 3 days, daily ones
 * otherwise; a margin of a fifth of its length on both sides (at least 1 day on hourly
 * bars, 10 on daily ones), never past now, and within the bars endpoint's limits (60
 * days of 1h, 3 years of 1d).
 */
export function tradeChartWindow(openedAt: string, closedAt: string | null, now: number = Date.now()): ChartWindow {
  const open = Date.parse(openedAt);
  const close = closedAt ? Date.parse(closedAt) : now;
  const length = Math.max(0, close - open);
  const timeframe = length < 3 * DAY ? "1h" : "1d";
  const margin = Math.max(length / 5, timeframe === "1h" ? DAY : 10 * DAY);
  let start = open - margin;
  const end = Math.min(now, close + margin);
  const max = timeframe === "1h" ? 60 * DAY : 3 * 365 * DAY;
  if (end - start > max) start = end - max;
  return { timeframe, start: new Date(start), end: new Date(Math.max(end, start + HOUR)) };
}

/**
 * The instrument sheet's lines: per portfolio holding it, its average price («Prezzo
 * medio») and the stop / target of its open exit orders; with more than one portfolio
 * each title names its portfolio.
 */
export function positionLines(
  positions: { portfolio_name: string; avg_price: string }[],
  orders: (OrderLike & { portfolio_name: string })[],
  money: (v: number) => string,
): PriceLine[] {
  const names = [...new Set([...positions.map((p) => p.portfolio_name), ...orders.map((o) => o.portfolio_name)])];
  const suffix = (name: string) => (names.length > 1 ? ` · ${name}` : "");
  const out: PriceLine[] = [];
  for (const name of names) {
    const pos = positions.find((p) => p.portfolio_name === name);
    const avg = pos ? Number(pos.avg_price) : NaN;
    if (Number.isFinite(avg) && avg > 0) out.push({ price: avg, kind: "entry", title: `Prezzo medio${suffix(name)}` });
    for (const l of protectionLines(orders.filter((o) => o.portfolio_name === name), money)) {
      out.push({ ...l, title: `${l.title}${suffix(name)}` });
    }
  }
  return out;
}

/** A price with its currency for the line titles: "182,50 $" (small prices: more decimals). */
export function chartMoney(v: number, currency: string): string {
  return Math.abs(v) < 1 ? `${priceLabel(v)} ${currency}` : formatMoney(v.toFixed(6), currency);
}
