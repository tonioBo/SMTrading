import { useQuery } from "@tanstack/react-query";
import { apiFetch } from "@/api/client";
import type { SignalsResponse } from "@/api/types";

export const signalsKey = ["signals"] as const;
/** one /api/signals call per minute per open tab (the server caches ~60 s too) */
export const SIGNALS_REFRESH_MS = 60_000;

export function useSignals() {
  return useQuery({
    queryKey: signalsKey,
    queryFn: () => apiFetch<SignalsResponse>("/api/signals"),
    refetchInterval: SIGNALS_REFRESH_MS,
    refetchIntervalInBackground: false,
    staleTime: SIGNALS_REFRESH_MS,
  });
}
