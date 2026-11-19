/** The «Andamento» chart lines of an `equity-history` response (EquityHistoryCard). */
import type { EquityHistory, EquityHistoryGoalLine } from "@/api/hooks/equityHistory";
import { cssColor, toSeconds, withAlpha, type Palette } from "@/lib/chartTheme";
import { benchmarkLabel, goalTargetLabel } from "@/lib/goals";
import { chartNumber, nearestIndex, signedNumber, toPctPoints, type ChartLine, type ChartPoint } from "@/lib/lineChart";

export type Unit = "eur" | "pct";

/** Series colours: distinct in light and dark mode; the single portfolio is green when up
 * over the range, red when down. */
export const SERIES_COLORS = ["#10b981", "#3b82f6", "#a855f7", "#f59e0b", "#ec4899", "#06b6d4", "#84cc16", "#f97316"];
export const GAIN_COLOR = "rgb(var(--gain))";
export const LOSS_COLOR = "rgb(var(--loss))";
export const GOAL_COLOR = "rgb(var(--warn))";
export const BENCHMARK_COLOR = "rgb(var(--muted))";
/** The «Totale» line (dashboard, more than one portfolio): neutral and thicker. */
export const TOTAL_COLOR = "rgb(var(--fg))";
export const TOTAL_KEY = "total";

/**
 * The portfolios summed, on the union of their timestamps, each carried forward from its
 * own last point; a portfolio counts from its first point in the range. As the total
 * goals (`goals/progress.py`): € = the sum of the counted portfolios' equity; % = that sum
 * over the sum of their first points − 1, so a portfolio appearing mid-range adds its
 * equity to both and makes no jump, and the last point is Σ last / Σ first − 1.
 */
export function totalPoints(series: ChartPoint[][], unit: Unit): ChartPoint[] {
  const times = [...new Set(series.flatMap((s) => s.map((p) => p.t)))].sort((a, b) => a - b);
  const at = series.map(() => -1);
  const out: ChartPoint[] = [];
  for (const t of times) {
    let sum = 0;
    let base = 0;
    series.forEach((s, i) => {
      while (at[i] + 1 < s.length && s[at[i] + 1].t <= t) at[i] += 1;
      if (at[i] < 0) return;
      sum += s[at[i]].v;
      base += s[0].v;
    });
    if (unit === "eur") out.push({ t, v: Number(sum.toFixed(2)) });
    else if (base > 0) out.push({ t, v: Number(((sum / base - 1) * 100).toPrecision(12)) });
  }
  return out;
}

/** A portfolio counted in the totals (`in_totals` not false). */
const inTotals = (p: { in_totals?: boolean }) => p.in_totals !== false;

/** The «Totale» row of the legend: Σ change over Σ first (the portfolios with data counted
 * in the totals). */
export function totalChange(data: EquityHistory): { change_eur: string; change_pct: string | null } | null {
  const counted = data.portfolios.filter((p) => inTotals(p) && p.series.length > 0 && p.first_eur != null && p.change_eur != null);
  if (counted.length < 2) return null;
  const cents = (v: string) => Math.round(Number(v) * 100);
  const change = counted.reduce((sum, p) => sum + cents(p.change_eur!), 0);
  const first = counted.reduce((sum, p) => sum + cents(p.first_eur!), 0);
  return { change_eur: (change / 100).toFixed(2), change_pct: first > 0 ? ((change / first) * 100).toFixed(2) : null };
}

/** A flat dashed line at `v` from `from` to `to` (none when the span is empty). */
function flat(key: string, label: string, tag: string, color: string, v: number, from: number, to: number): ChartLine | null {
  if (!Number.isFinite(v) || to < from) return null;
  return { key, label, tag, color, points: [{ t: from, v }, { t: to, v }], reference: true };
}

/**
 * The chart lines of a response: each portfolio (in € or % since its first point), the
 * «Totale» of the portfolios counted in the totals (dashboard with more than one:
 * `totalPoints`, bold; a portfolio with `in_totals` false is drawn but not summed, and the
 * line is then labelled «Totale (obiettivi)»), then
 * the goals, dashed and labelled, from the period start (or the first point) to now (or
 * the period end).
 * - Dashboard: the `goals` list only (the weekly goal is its total goal there, so the
 *   per-portfolio weekly `goal` would duplicate it). A total goal is drawn against the
 *   «Totale»: in % relative to the counted portfolios' total at their first points, in €
 *   at its target; a portfolio goal in the portfolio's colour.
 * - Portfolio detail: the portfolio's weekly `goal` («Sett.» and «1s»: the list leaves the
 *   total goals out there) and its own goals from the list.
 * - An index goal (beat_index) has no flat line: its index is drawn instead, in % only,
 *   as a thin solid line tagged with the symbol (the `benchmarks` series, rebased to its
 *   first point in the range like the portfolios: v' = ((1 + v) / (1 + v0) − 1)).
 */
export function historyLines(data: EquityHistory, unit: Unit, single: boolean): ChartLine[] {
  const end = Date.parse(data.end);
  const lines: ChartLine[] = [];
  const firsts = new Map<string, ChartPoint>();
  /** the first points of the portfolios counted in the totals: the total goals' base */
  const countedFirsts: ChartPoint[] = [];
  const names = new Map<string, string>();
  const eurSeries: ChartPoint[][] = [];
  data.portfolios
    .filter((p) => p.series.length > 0)
    .forEach((p, i) => {
      const eur: ChartPoint[] = p.series.map((s) => ({ t: Date.parse(s.ts), v: Number(s.equity_eur) }));
      const up = eur.length === 0 || eur[eur.length - 1].v >= eur[0].v;
      const color = single ? (up ? GAIN_COLOR : LOSS_COLOR) : SERIES_COLORS[i % SERIES_COLORS.length];
      if (inTotals(p)) eurSeries.push(eur);
      const points = unit === "pct" ? toPctPoints(eur) : eur;
      if (points.length === 0) return;
      lines.push({ key: p.id, label: p.name, color, points });
      firsts.set(p.id, eur[0]);
      if (inTotals(p)) countedFirsts.push(eur[0]);
      names.set(p.id, p.name);
      if (!single || !p.goal) return;
      const target = Number(p.goal.target_eur);
      const v = unit === "pct" ? (target / eur[0].v - 1) * 100 : target;
      const from = Math.max(Date.parse(p.goal.week_start), eur[0].t);
      const tag = `settimanale ${goalTargetLabel("min", data.goal_pct ?? "0")}`;
      const line = flat(`${p.id}-goal`, `Obiettivo ${tag}`, tag, GOAL_COLOR, v, from, Math.max(end, from));
      if (line) lines.push(line);
    });

  if (!single && eurSeries.length > 1) {
    const points = totalPoints(eurSeries, unit);
    // «Totale (obiettivi)» when a portfolio with data is left out of it (out of the goals)
    const label = data.portfolios.some((p) => p.series.length > 0 && !inTotals(p)) ? "Totale (obiettivi)" : "Totale";
    if (points.length > 0) lines.push({ key: TOTAL_KEY, label, color: TOTAL_COLOR, points, bold: true });
  }
  const hasTotal = lines.some((l) => l.key === TOTAL_KEY);

  const colorOf = new Map(lines.map((l) => [l.key, l.color]));
  for (const g of data.goals ?? []) {
    const line = goalLine(g);
    if (line) lines.push(line);
  }
  if (unit === "pct") {
    for (const [symbol, series] of Object.entries(data.benchmarks ?? {})) {
      const raw = series.map((p) => ({ t: Date.parse(p.ts), v: Number(p.pct) })).filter((p) => Number.isFinite(p.v));
      if (raw.length === 0) continue;
      // rebased to its first point in the range, so index and portfolios share the start
      const b0 = 1 + raw[0].v / 100;
      const points = b0 > 0 && raw[0].v !== 0 ? raw.map((p) => ({ t: p.t, v: ((1 + p.v / 100) / b0 - 1) * 100 })) : raw;
      const short = benchmarkLabel(symbol);
      lines.push({ key: `bench-${symbol}`, label: `Indice ${short}`, tag: short, color: BENCHMARK_COLOR, points, thin: true });
    }
  }
  return lines;

  function goalLine(g: EquityHistoryGoalLine): ChartLine | null {
    if (g.target_eur == null || g.kind === "beat_index") return null;
    const target = Number(g.target_eur);
    const to = Math.min(Date.parse(g.period_end), end);
    const tag = goalTargetLabel(g.kind, g.target_pct, g.benchmark);
    if (g.scope === "total") {
      if (single || countedFirsts.length === 0 || (unit !== "pct" && !hasTotal)) return null;
      const all = countedFirsts;
      const base = all.reduce((sum, p) => sum + p.v, 0);
      const from = Math.max(Date.parse(g.period_start), Math.min(...all.map((p) => p.t)));
      const text = `${g.name} ${tag}`;
      const v = unit === "pct" ? (target / base - 1) * 100 : target;
      return flat(`goal-${g.goal_id}`, `Obiettivo ${text}`, text, GOAL_COLOR, v, from, to);
    }
    const first = g.portfolio_id == null ? undefined : firsts.get(g.portfolio_id);
    if (!first) return null;
    const v = unit === "pct" ? (target / first.v - 1) * 100 : target;
    const from = Math.max(Date.parse(g.period_start), first.t);
    const text = single ? `${g.name} ${tag}` : `${g.name} ${tag} · ${names.get(g.portfolio_id!) ?? ""}`;
    return flat(`goal-${g.goal_id}`, `Obiettivo ${text}`, text, single ? GOAL_COLOR : (colorOf.get(g.portfolio_id!) ?? GOAL_COLOR), v, from, to);
  }
}

// ---------------------------------------------------------------- the chart's series

export interface SeriesPoint {
  time: number; // UTC seconds
  value: number;
}

export interface EquitySeries {
  key: string;
  /** area: a portfolio (gradient); line: the «Totale» (bold) or an index (thin); goal: a
   * hidden flat series carrying the goal's labelled price line (and the autoscale) */
  kind: "area" | "line" | "goal";
  color: string;
  lineWidth: 1 | 2 | 3;
  topColor?: string;
  bottomColor?: string;
  data: SeriesPoint[];
  priceLine?: { price: number; title: string; color: string };
}

/** Points as chart data: UTC seconds, ascending, one per second (the last wins). */
export function toSeriesData(points: ChartPoint[]): SeriesPoint[] {
  const byTime = new Map<number, number>();
  for (const p of points) if (Number.isFinite(p.t) && Number.isFinite(p.v)) byTime.set(toSeconds(p.t), p.v);
  return [...byTime.entries()].sort((a, b) => a[0] - b[0]).map(([time, value]) => ({ time, value }));
}

/**
 * The lines as chart series, drawn in this order: the portfolios (areas with a gradient:
 * stronger with one portfolio), the indices (thin), the «Totale» (bold, on top), then the
 * goals (labelled dashed price lines). Colours resolved for the canvas.
 */
export function equitySeries(lines: ChartLine[], palette: Palette, single: boolean): EquitySeries[] {
  const rank = (l: ChartLine) => (l.reference ? 3 : l.bold ? 2 : l.thin ? 1 : 0);
  return [...lines]
    .filter((l) => l.points.length > 0)
    .sort((a, b) => rank(a) - rank(b))
    .map((l) => {
      const color = cssColor(l.color, palette);
      const data = toSeriesData(l.points);
      if (l.reference) {
        return { key: l.key, kind: "goal", color, lineWidth: 1, data, priceLine: { price: data[0]?.value ?? 0, title: l.tag ?? l.label, color } };
      }
      if (l.bold || l.thin) return { key: l.key, kind: "line", color, lineWidth: l.bold ? 3 : 1, data };
      return {
        key: l.key,
        kind: "area",
        color,
        lineWidth: 2,
        topColor: withAlpha(color, single ? 0.35 : 0.12),
        bottomColor: withAlpha(color, 0),
        data,
      };
    });
}

export interface TooltipRow {
  key: string;
  label: string;
  color: string;
  /** the value: euros where known (an index: its %) */
  value: string;
  /** the change since the range start: "+5,00 € · +2,00%" (null for an index) */
  change: string | null;
  bold?: boolean;
}

/**
 * The crosshair tooltip at `t`: every drawn series (not the goals), in the order of
 * `lines`, at its point closest in time, with its value in euros and its change since
 * the range start in € and % (from the same response in both units).
 */
export function tooltipRows(lines: ChartLine[], eurLines: ChartLine[], pctLines: ChartLine[], t: number): TooltipRow[] {
  const eur = new Map(eurLines.map((l) => [l.key, l.points]));
  const pct = new Map(pctLines.map((l) => [l.key, l.points]));
  const at = (pts: ChartPoint[] | undefined) => (pts && pts.length > 0 ? pts[nearestIndex(pts, t)] : undefined);
  const rows: TooltipRow[] = [];
  for (const l of lines) {
    if (l.reference || l.points.length === 0) continue;
    const e = at(eur.get(l.key));
    const p = at(pct.get(l.key));
    const first = eur.get(l.key)?.[0];
    let value: string;
    let change: string | null = null;
    if (e) {
      value = `${chartNumber(e.v, 2)} €`;
      const parts = [first ? `${signedNumber(e.v - first.v, 2)} €` : null, p ? `${signedNumber(p.v, 2)}%` : null].filter(Boolean);
      change = parts.length > 0 ? parts.join(" · ") : null;
    } else if (p) value = `${signedNumber(p.v, 2)}%`;
    else continue;
    rows.push({ key: l.key, label: l.label, color: l.color, value, change, bold: l.bold });
  }
  return rows;
}
