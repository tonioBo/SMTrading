import { useQuery } from "@tanstack/react-query";
import { useRef } from "react";
import { apiFetch } from "@/api/client";
import type { FlowEvent, FlowEventsResponse, FlowMap, FlowSummary, FlowWindow } from "@/api/types";

/** «Fiume dei flussi»: the events are read every 2 s, the summary every 10 s, the map
 * (nodes and links, from the backend only) every minute. Polling stops while the browser
 * tab is hidden (`refetchIntervalInBackground: false` follows `document.visibilityState`). */
export const FLOW_EVENTS_MS = 2_000;
export const FLOW_SUMMARY_MS = 10_000;
export const FLOW_MAP_MS = 60_000;
/** ids remembered to drop the events a poll re-reads (the server overlaps 10 s) */
const SEEN_MAX = 5_000;

export function useFlowMap() {
  return useQuery({
    queryKey: ["flow", "map"],
    queryFn: () => apiFetch<FlowMap>("/api/flow/map"),
    staleTime: FLOW_MAP_MS,
    refetchInterval: FLOW_MAP_MS,
    refetchIntervalInBackground: false,
  });
}

export function useFlowSummary(window: FlowWindow) {
  return useQuery({
    queryKey: ["flow", "summary", window],
    queryFn: () => apiFetch<FlowSummary>(`/api/flow/summary?window=${window}`),
    staleTime: FLOW_SUMMARY_MS,
    refetchInterval: FLOW_SUMMARY_MS,
    refetchIntervalInBackground: false,
  });
}

interface EventsBatch {
  /** new since the previous poll, deduped by id, oldest first */
  events: FlowEvent[];
  /** the server clock at the poll */
  now: string;
}

export interface FlowEventsState {
  events: FlowEvent[];
  now: string | null;
  /** the last poll failed: the page keeps what it has and tries again */
  stale: boolean;
}

const NO_EVENTS: FlowEvent[] = [];

/** The events since the last poll: the time cursor and the ids already seen live in refs,
 * so each poll asks `after=<cursor>` and hands over only what is new. */
export function useFlowEvents(): FlowEventsState {
  const cursor = useRef<string | null>(null);
  const seen = useRef<Set<string>>(new Set());
  const query = useQuery({
    queryKey: ["flow", "events"],
    queryFn: async (): Promise<EventsBatch> => {
      if (typeof document !== "undefined" && document.visibilityState === "hidden") {
        return { events: NO_EVENTS, now: "" }; // nothing read: no server clock to hand over
      }
      const qs = cursor.current ? `?after=${encodeURIComponent(cursor.current)}` : "";
      const res = await apiFetch<FlowEventsResponse>(`/api/flow/events${qs}`);
      cursor.current = res.cursor;
      const fresh = res.events.filter((e) => !seen.current.has(e.id));
      fresh.forEach((e) => seen.current.add(e.id));
      if (seen.current.size > SEEN_MAX) {
        // keep the newest half: older ids are far behind the 10 s overlap
        seen.current = new Set([...seen.current].slice(-SEEN_MAX / 2));
      }
      return { events: fresh, now: res.now };
    },
    refetchInterval: FLOW_EVENTS_MS,
    refetchIntervalInBackground: false,
    staleTime: 0,
    gcTime: 0, // a page opened again starts from the last 15 minutes with fresh refs
    retry: false, // the next poll is the retry
    structuralSharing: false, // every batch is new, even when it looks like the previous one
  });
  return {
    events: query.data?.events ?? NO_EVENTS,
    now: query.data?.now || null,
    stale: query.isError,
  };
}
