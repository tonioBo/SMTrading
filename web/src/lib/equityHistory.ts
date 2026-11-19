/** The «Andamento» chart lines of an `equity-history` response (EquityHistoryCard). */
import type { EquityHistory, EquityHistoryGoalLine } from "@/api/hooks/equityHistory";
import type { ChartLine } from "@/components/LineChart";
import { benchmarkLabel, goalTargetLabel } from "@/lib/goals";
import { toPctPoints, type ChartPoint } from "@/lib/lineChart";

export type Unit = "eur" | "pct";

/** Series colours: distinct in light and dark mode; the single-portfolio line is the accent. */
export const SERIES_COLORS = ["#10b981", "#3b82f6", "#a855f7", "#f59e0b", "#ec4899", "#06b6d4", "#84cc16", "#f97316"];
export const ACCENT = "rgb(var(--accent))";
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
      const color = single ? ACCENT : SERIES_COLORS[i % SERIES_COLORS.length];
      const eur: ChartPoint[] = p.series.map((s) => ({ t: Date.parse(s.ts), v: Number(s.equity_eur) }));
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
