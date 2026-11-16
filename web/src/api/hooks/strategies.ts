import { useQuery } from "@tanstack/react-query";
import { apiFetch } from "@/api/client";
import type { StrategiesPerformanceResponse } from "@/api/types";

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
