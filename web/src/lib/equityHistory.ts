/** The «Andamento» chart lines of an `equity-history` response (EquityHistoryCard). */
import type { EquityHistory } from "@/api/hooks/equityHistory";
import type { ChartLine } from "@/components/LineChart";
import { toPctPoints, type ChartPoint } from "@/lib/lineChart";

export type Unit = "eur" | "pct";

/** Series colours: distinct in light and dark mode; the single-portfolio line is the accent. */
export const SERIES_COLORS = ["#10b981", "#3b82f6", "#a855f7", "#f59e0b", "#ec4899", "#06b6d4", "#84cc16", "#f97316"];
export const ACCENT = "rgb(var(--accent))";
export const GOAL_COLOR = "rgb(var(--warn))";

/** The chart lines of a response: each portfolio (in € or % since its first point) and,
 * for «1s», its weekly goal as a dashed line from Monday (or the range start) to now. */
export function historyLines(data: EquityHistory, unit: Unit, single: boolean): ChartLine[] {
  const end = Date.parse(data.end);
  const lines: ChartLine[] = [];
  data.portfolios
    .filter((p) => p.series.length > 0)
    .forEach((p, i) => {
      const color = single ? ACCENT : SERIES_COLORS[i % SERIES_COLORS.length];
      const eur: ChartPoint[] = p.series.map((s) => ({ t: Date.parse(s.ts), v: Number(s.equity_eur) }));
      const points = unit === "pct" ? toPctPoints(eur) : eur;
      if (points.length === 0) return;
      lines.push({ key: p.id, label: p.name, color, points });
      if (!p.goal) return;
      const target = Number(p.goal.target_eur);
      const v = unit === "pct" ? (target / eur[0].v - 1) * 100 : target;
      const from = Math.max(Date.parse(p.goal.week_start), eur[0].t);
      lines.push({
        key: `${p.id}-goal`,
        label: `Obiettivo ${p.name}`,
        color: single ? GOAL_COLOR : color,
        points: [{ t: from, v }, { t: Math.max(end, from), v }],
        reference: true,
      });
    });
  return lines;
}
