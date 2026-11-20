import { useQuery } from "@tanstack/react-query";
import { apiFetch } from "@/api/client";
import type { ResearchRun, ResearchRunsResponse, StrategiesPerformanceResponse, StrategyExpectationsResponse } from "@/api/types";

export const strategiesPerformanceKey = ["strategies", "performance"] as const;
export const STRATEGIES_REFRESH_MS = 60_000;

export function useStrategiesPerformance() {
  return useQuery({
    queryKey: strategiesPerformanceKey,
    queryFn: () => apiFetch<StrategiesPerformanceResponse>("/api/strategies/performance"),
    refetchInterval: STRATEGIES_REFRESH_MS,
    refetchIntervalInBackground: false,
    staleTime: STRATEGIES_REFRESH_MS,
  });
}

export const strategyExpectationsKey = ["strategies", "expectations"] as const;

export function useStrategyExpectations(enabled = true) {
  return useQuery({
    queryKey: strategyExpectationsKey,
    queryFn: () => apiFetch<StrategyExpectationsResponse>("/api/strategies/expectations"),
    refetchInterval: STRATEGIES_REFRESH_MS,
    refetchIntervalInBackground: false,
    staleTime: STRATEGIES_REFRESH_MS,
    enabled,
  });
}

export const researchRunsKey = ["strategies", "research"] as const;

export function useResearchRuns(enabled = true) {
  return useQuery({
    queryKey: researchRunsKey,
    queryFn: () => apiFetch<ResearchRunsResponse>("/api/strategies/research"),
    refetchInterval: STRATEGIES_REFRESH_MS,
    refetchIntervalInBackground: false,
    staleTime: STRATEGIES_REFRESH_MS,
    enabled,
  });
}

export function useResearchRun(id: string | null) {
  return useQuery({
    queryKey: [...researchRunsKey, id],
    queryFn: () => apiFetch<ResearchRun>(`/api/strategies/research/${id}`),
    enabled: id != null,
    staleTime: STRATEGIES_REFRESH_MS,
  });
}
