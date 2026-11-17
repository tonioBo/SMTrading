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

/** A flat dashed line at `v` from `from` to `to` (none when the span is empty). */
function flat(key: string, label: string, tag: string, color: string, v: number, from: number, to: number): ChartLine | null {
  if (!Number.isFinite(v) || to < from) return null;
  return { key, label, tag, color, points: [{ t: from, v }, { t: to, v }], reference: true };
}

/**
 * The chart lines of a response: each portfolio (in € or % since its first point), then
 * the goals, dashed and labelled, from the period start (or the first point) to now (or
 * the period end).
 * - Dashboard: the `goals` list only (the weekly goal is its total goal there, so the
 *   per-portfolio weekly `goal` would duplicate it). A total goal is drawn in % only,
 *   against the shown portfolios' total at their first points; a portfolio goal in the
 *   portfolio's colour.
 * - Portfolio detail: the portfolio's weekly `goal` («1s», as before: the list leaves the
 *   total goals out there) and its own goals from the list.
 * - An index goal (beat_index) has no flat line: its index is drawn instead, in % only,
 *   as a thin solid line tagged with the symbol (the `benchmarks` series, rebased to its
 *   first point in the range like the portfolios: v' = ((1 + v) / (1 + v0) − 1)).
 */
export function historyLines(data: EquityHistory, unit: Unit, single: boolean): ChartLine[] {
  const end = Date.parse(data.end);
  const lines: ChartLine[] = [];
  const firsts = new Map<string, ChartPoint>();
  const names = new Map<string, string>();
  data.portfolios
    .filter((p) => p.series.length > 0)
    .forEach((p, i) => {
      const color = single ? ACCENT : SERIES_COLORS[i % SERIES_COLORS.length];
      const eur: ChartPoint[] = p.series.map((s) => ({ t: Date.parse(s.ts), v: Number(s.equity_eur) }));
      const points = unit === "pct" ? toPctPoints(eur) : eur;
      if (points.length === 0) return;
      lines.push({ key: p.id, label: p.name, color, points });
      firsts.set(p.id, eur[0]);
      names.set(p.id, p.name);
      if (!single || !p.goal) return;
      const target = Number(p.goal.target_eur);
      const v = unit === "pct" ? (target / eur[0].v - 1) * 100 : target;
      const from = Math.max(Date.parse(p.goal.week_start), eur[0].t);
      const tag = `settimanale ${goalTargetLabel("min", data.goal_pct ?? "0")}`;
      const line = flat(`${p.id}-goal`, `Obiettivo ${tag}`, tag, GOAL_COLOR, v, from, Math.max(end, from));
      if (line) lines.push(line);
    });

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
      if (single || unit !== "pct" || firsts.size === 0) return null;
      const all = [...firsts.values()];
      const base = all.reduce((sum, p) => sum + p.v, 0);
      const from = Math.max(Date.parse(g.period_start), Math.min(...all.map((p) => p.t)));
      const text = `${g.name} ${tag}`;
      return flat(`goal-${g.goal_id}`, `Obiettivo ${text}`, text, GOAL_COLOR, (target / base - 1) * 100, from, to);
    }
    const first = g.portfolio_id == null ? undefined : firsts.get(g.portfolio_id);
    if (!first) return null;
    const v = unit === "pct" ? (target / first.v - 1) * 100 : target;
    const from = Math.max(Date.parse(g.period_start), first.t);
    const text = single ? `${g.name} ${tag}` : `${g.name} ${tag} · ${names.get(g.portfolio_id!) ?? ""}`;
    return flat(`goal-${g.goal_id}`, `Obiettivo ${text}`, text, single ? GOAL_COLOR : (colorOf.get(g.portfolio_id!) ?? GOAL_COLOR), v, from, to);
  }
}
