import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import { apiFetch } from "@/api/client";
import type { Dashboard, PortfolioMode } from "@/api/types";
import { throttle } from "@/lib/throttle";
import { useLiveMessage, useLiveSocket, type LiveEvent } from "@/lib/ws";

export const dashboardKey = ["dashboard"] as const;
/** under dashboardKey: invalidating the dashboard refreshes the banner too */
export const modesKey = ["dashboard", "modes"] as const;
export const MODES_STALE_MS = 60_000;
export const FEED_SIZE = 50;

/** Live events shown in the feed: orders and fills. */
export const FEED_EVENT_LABELS: Record<string, string> = {
  "order.filled": "Eseguito",
  "order.canceled": "Annullato",
  "order.expired": "Scaduto",
  "order.rejected": "Rifiutato",
};

/** at most one dashboard refetch per window while ticks or events arrive */
export const LIVE_REFETCH_MS = 3000;
/** polling while the WebSocket is not connected */
export const FALLBACK_POLL_MS = 30_000;
/** polling while it is: a dead worker or a quiet market sends nothing, but the heartbeat
 * and connector statuses must still move */
export const CONNECTED_POLL_MS = 45_000;

/** Events that change what the dashboard shows (orders, cash, portfolios, broker, risk). */
const RELEVANT_EVENT =
  /^(order\.|portfolio\.|position|journal|deposit|withdraw|reset|broker|reconcil|notification|price_alert|daily_loss|margin\.|liquidation|kill_switch)/;

export function isDashboardEvent(type: string): boolean {
  return RELEVANT_EVENT.test(type);
}

/**
 * Real-time dashboard summary: quotes of the held symbols (WS) and every relevant event
 * trigger a throttled refetch (leading + trailing, at most one per LIVE_REFETCH_MS);
 * polling every 45 s while the WS is connected (heartbeat, connectors), 30 s when not.
 */
export function useDashboard() {
  const qc = useQueryClient();
  const { socket, status } = useLiveSocket();
  const refresh = useMemo(
    () => throttle(() => void qc.invalidateQueries({ queryKey: dashboardKey, exact: true }), LIVE_REFETCH_MS),
    [qc],
  );
  useEffect(() => () => refresh.cancel(), [refresh]);
  useLiveMessage("event", (event) => {
    if (!isDashboardEvent(event.type)) return;
    if (event.type.startsWith("portfolio.") || event.type.startsWith("broker")) {
      void qc.invalidateQueries({ queryKey: modesKey });
    }
    refresh.call();
  });
  useLiveMessage("quote", () => refresh.call());
  const query = useQuery({
    queryKey: dashboardKey,
    queryFn: () => apiFetch<Dashboard>("/api/dashboard"),
    refetchInterval: status === "open" ? CONNECTED_POLL_MS : FALLBACK_POLL_MS,
  });
  const held = (query.data?.held_symbols ?? []).join(",");
  useEffect(() => {
    if (!socket || !held) return;
    return socket.subscribeQuotes(held.split(","));
  }, [socket, held]);
  return { ...query, live: status === "open" };
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

/** The last FEED_SIZE order/fill events received while mounted, newest first. */
export function useLiveFeed(): LiveEvent[] {
  const [events, setEvents] = useState<LiveEvent[]>([]);
  useLiveMessage("event", (event) => {
    if (event.type in FEED_EVENT_LABELS) setEvents((prev) => [event, ...prev].slice(0, FEED_SIZE));
  });
  return events;
}
