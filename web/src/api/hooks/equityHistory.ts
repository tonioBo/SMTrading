import { useQuery } from "@tanstack/react-query";
import { apiFetch } from "@/api/client";
import type { EquityPoint, GoalBenchmark, GoalHorizon, GoalKind, GoalScope } from "@/api/types";

export type EquityHistoryRange = "2h" | "6h" | "12h" | "1d" | "1w" | "1m" | "3m" | "all";

/** Ranges drawn with hours on the x axis (the rest: dates). */
export const HOURLY_RANGES: ReadonlySet<EquityHistoryRange> = new Set(["2h", "6h", "12h", "1d"]);

export const EQUITY_HISTORY_RANGES: { label: string; value: EquityHistoryRange }[] = [
  { label: "2h", value: "2h" },
  { label: "6h", value: "6h" },
  { label: "12h", value: "12h" },
  { label: "1g", value: "1d" },
  { label: "1s", value: "1w" },
  { label: "1m", value: "1m" },
  { label: "3m", value: "3m" },
  { label: "tutto", value: "all" },
];

/** The weekly goal of a portfolio («1s» only, while the goal is on). */
export interface EquityHistoryGoal {
  week_start: string;
  base_eur: string;
  target_eur: string;
}

export interface EquityHistoryPortfolio {
  id: string;
  name: string;
  series: EquityPoint[];
  first_eur: string | null;
  last_eur: string | null;
  change_eur: string | null;
  change_pct: string | null;
  goal: EquityHistoryGoal | null;
}

/** An active goal whose current period overlaps the range (every range): dashboard =
 * total goals and the listed portfolios' goals; one portfolio = its goals only.
 * `period_start`/`period_end`: ISO instants of the local midnights opening/closing it. */
export interface EquityHistoryGoalLine {
  goal_id: string;
  name: string;
  horizon: GoalHorizon;
  scope: GoalScope;
  portfolio_id: string | null;
  kind: GoalKind;
  /** beat_index only (absent from older servers) */
  benchmark?: GoalBenchmark | null;
  target_pct: string;
  period_start: string;
  period_end: string;
  base_eur: string | null;
  target_eur: string | null;
}

export interface EquityHistory {
  range: EquityHistoryRange;
  start: string | null;
  end: string;
  goal_pct: string | null;
  portfolios: EquityHistoryPortfolio[];
  /** absent from older servers */
  goals?: EquityHistoryGoalLine[];
  /** the index of each beat_index goal overlapping the range: its % in EUR over time */
  benchmarks?: Record<string, { ts: string; pct: string }[]>;
}

/** New snapshots arrive every 5 minutes: refresh at that pace. */
export const EQUITY_HISTORY_REFRESH_MS = 5 * 60_000;

/** `GET /api/portfolios/equity-history`: every live portfolio, or one (`portfolioId`). */
export function useEquityHistory(range: EquityHistoryRange, portfolioId?: string) {
  const qs = new URLSearchParams({ range });
  if (portfolioId) qs.set("portfolio_id", portfolioId);
  return useQuery({
    queryKey: ["equity-history", portfolioId ?? "all", range],
    queryFn: () => apiFetch<EquityHistory>(`/api/portfolios/equity-history?${qs}`),
    refetchInterval: EQUITY_HISTORY_REFRESH_MS,
  });
}
