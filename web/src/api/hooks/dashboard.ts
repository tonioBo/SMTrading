import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { apiFetch } from "@/api/client";
import type { Dashboard, EquityPeriod, EquitySeries, PortfolioMode } from "@/api/types";
import { useLiveMessage, type LiveEvent } from "@/lib/ws";

export const dashboardKey = ["dashboard"] as const;
/** under dashboardKey: invalidating the dashboard refreshes the banner too */
export const modesKey = ["dashboard", "modes"] as const;
export const MODES_STALE_MS = 60_000;
/** Period selector labels mapped to the API periods. */
export const EQUITY_PERIODS: { label: string; value: EquityPeriod }[] = [
  { label: "1g", value: "1d" },
  { label: "1s", value: "1w" },
  { label: "1m", value: "1m" },
  { label: "3m", value: "3m" },
  { label: "1a", value: "1y" },
  { label: "tutto", value: "all" },
];

export const FEED_SIZE = 50;

/** Live events shown in the feed: orders and fills. */
export const FEED_EVENT_LABELS: Record<string, string> = {
  "order.filled": "Eseguito",
  "order.canceled": "Annullato",
  "order.expired": "Scaduto",
  "order.rejected": "Rifiutato",
};

/** Dashboard summary; refreshed when a fill, a margin event or the kill-switch arrives. */
export function useDashboard() {
  const qc = useQueryClient();
  useLiveMessage("event", (event) => {
    if (event.type === "order.filled" || event.type === "kill_switch" || event.type.startsWith("margin.")) {
      void qc.invalidateQueries({ queryKey: dashboardKey });
    }
  });
  return useQuery({
    queryKey: dashboardKey,
    queryFn: () => apiFetch<Dashboard>("/api/dashboard"),
    refetchInterval: 30_000,
  });
}

/** Live portfolios per mode (cheap endpoint, no valuation) for the global banner. */
export function useModes() {
  return useQuery({
    queryKey: modesKey,
    queryFn: () => apiFetch<Record<PortfolioMode, number>>("/api/dashboard/modes"),
    staleTime: MODES_STALE_MS,
    refetchInterval: MODES_STALE_MS,
  });
}

export function useEquity(period: EquityPeriod) {
  return useQuery({
    queryKey: ["dashboard", "equity", period],
    queryFn: () => apiFetch<EquitySeries>(`/api/dashboard/equity?period=${period}`),
  });
}

/** The last FEED_SIZE order/fill events received while mounted, newest first. */
export function useLiveFeed(): LiveEvent[] {
  const [events, setEvents] = useState<LiveEvent[]>([]);
  useLiveMessage("event", (event) => {
    if (event.type in FEED_EVENT_LABELS) setEvents((prev) => [event, ...prev].slice(0, FEED_SIZE));
  });
  return events;
}
