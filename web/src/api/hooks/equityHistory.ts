import { useQuery } from "@tanstack/react-query";
import { apiFetch } from "@/api/client";
import type { EquityPoint } from "@/api/types";

export type EquityHistoryRange = "1d" | "1w" | "1m" | "3m" | "all";

export const EQUITY_HISTORY_RANGES: { label: string; value: EquityHistoryRange }[] = [
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

export interface EquityHistory {
  range: EquityHistoryRange;
  start: string | null;
  end: string;
  goal_pct: string | null;
  portfolios: EquityHistoryPortfolio[];
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
