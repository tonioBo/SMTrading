import { useQuery } from "@tanstack/react-query";
import { apiFetch } from "@/api/client";
import type { OpenPositionsResponse } from "@/api/types";

export const openPositionsKey = ["positions", "open"] as const;
/** «Posizioni» refreshes once a minute while the tab is visible */
export const POSITIONS_REFRESH_MS = 60_000;

export function useOpenPositions() {
  return useQuery({
    queryKey: openPositionsKey,
    queryFn: () => apiFetch<OpenPositionsResponse>("/api/positions/open"),
    refetchInterval: POSITIONS_REFRESH_MS,
    refetchIntervalInBackground: false,
    staleTime: POSITIONS_REFRESH_MS / 2,
  });
}
