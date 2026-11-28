import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { apiFetch } from "@/api/client";
import type {
  FlowAgents,
  FlowEvent,
  FlowEventsResponse,
  FlowHealth,
  FlowMap,
  FlowMcpStory,
  FlowOrderStory,
  FlowReplayPage,
  FlowSources,
  FlowSummary,
  FlowTools,
  FlowWindow,
} from "@/api/types";

/** «Fiume dei flussi»: the events are read every 2 s, the summary every 10 s, the map
 * (nodes and links, from the backend only) every minute. Polling stops while the browser
 * tab is hidden (`refetchIntervalInBackground: false` follows `document.visibilityState`). */
export const FLOW_EVENTS_MS = 2_000;
export const FLOW_SUMMARY_MS = 10_000;
export const FLOW_MAP_MS = 60_000;
/** v2: Paperclip runs and costs (the server caches 30 s), node health (15 s), price
 * sources and the lens' tools (10 s, only while they are shown) */
export const FLOW_AGENTS_MS = 30_000;
export const FLOW_HEALTH_MS = 15_000;
export const FLOW_DETAIL_MS = 10_000;
/** a replay page; the window stops loading beyond REPLAY_EVENTS_MAX events */
export const REPLAY_PAGE = 1_000;
export const REPLAY_EVENTS_MAX = 50_000;
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

/** the tools by family (the lens) and per link (the link card); read only while shown */
export function useFlowTools(window: FlowWindow, enabled: boolean) {
  return useQuery({
    queryKey: ["flow", "tools", window],
    queryFn: () => apiFetch<FlowTools>(`/api/flow/tools?window=${window}`),
    enabled,
    staleTime: FLOW_DETAIL_MS,
    refetchInterval: enabled ? FLOW_DETAIL_MS : false,
    refetchIntervalInBackground: false,
    placeholderData: keepPreviousData,
  });
}

/** Paperclip runs (impulses Paperclip → agent) and the agents' spend; always answers,
 * `available: false` when Paperclip is down */
export function useFlowAgents() {
  return useQuery({
    queryKey: ["flow", "agents"],
    queryFn: () => apiFetch<FlowAgents>("/api/flow/agents"),
    staleTime: FLOW_AGENTS_MS,
    refetchInterval: FLOW_AGENTS_MS,
    refetchIntervalInBackground: false,
  });
}

export function useFlowHealth() {
  return useQuery({
    queryKey: ["flow", "health"],
    queryFn: () => apiFetch<FlowHealth>("/api/flow/health"),
    staleTime: FLOW_HEALTH_MS,
    refetchInterval: FLOW_HEALTH_MS,
    refetchIntervalInBackground: false,
  });
}

/** the price sources (the «Prezzi» lens), read only while the lens is open */
export function useFlowSources(enabled: boolean) {
  return useQuery({
    queryKey: ["flow", "sources"],
    queryFn: () => apiFetch<FlowSources>("/api/flow/sources"),
    enabled,
    staleTime: FLOW_DETAIL_MS,
    refetchInterval: enabled ? FLOW_DETAIL_MS : false,
    refetchIntervalInBackground: false,
  });
}

export function useFlowOrderStory(id: string | null) {
  return useQuery({
    queryKey: ["flow", "order", id],
    queryFn: () => apiFetch<FlowOrderStory>(`/api/flow/order/${encodeURIComponent(id!)}`),
    enabled: id !== null,
    retry: false,
  });
}

export function useFlowMcpStory(id: string | null) {
  return useQuery({
    queryKey: ["flow", "mcp", id],
    queryFn: () => apiFetch<FlowMcpStory>(`/api/flow/mcp/${encodeURIComponent(id!)}`),
    enabled: id !== null,
    retry: false,
  });
}

export interface FlowReplayState {
  /** every event loaded so far, oldest first (a new array per page) */
  events: FlowEvent[];
  /** pages read */
  pages: number;
  /** every page read, or stopped (error, REPLAY_EVENTS_MAX) */
  done: boolean;
  /** beyond REPLAY_EVENTS_MAX: the replay shows the first events only */
  capped: boolean;
  error: string | null;
}

const IDLE: FlowReplayState = { events: NO_EVENTS, pages: 0, done: false, capped: false, error: null };
const rangeKey = (from: number | null, to: number | null) => (from === null || to === null ? null : `${from}|${to}`);

/** A closed window (≤ 24 h), page by page with the server's cursor: each page is handed
 * over as it arrives, so the replay starts on the first one. A new range aborts the old. */
export function useFlowReplay(range: { from: number; to: number } | null): FlowReplayState {
  // the state is tagged with its range: a new range never sees the previous one's events
  const [state, setState] = useState<{ key: string | null; s: FlowReplayState }>({ key: null, s: IDLE });
  const from = range?.from ?? null;
  const to = range?.to ?? null;
  const key = rangeKey(from, to);
  useEffect(() => {
    if (from === null || to === null) return;
    const mine = rangeKey(from, to);
    const ctl = new AbortController();
    const base = `from=${encodeURIComponent(new Date(from).toISOString())}&to=${encodeURIComponent(new Date(to).toISOString())}&limit=${REPLAY_PAGE}`;
    (async () => {
      let all: FlowEvent[] = [];
      let cursor: string | null = null;
      let pages = 0;
      try {
        do {
          const qs: string = cursor ? `${base}&cursor=${encodeURIComponent(cursor)}` : base;
          const res: FlowReplayPage = await apiFetch<FlowReplayPage>(`/api/flow/events?${qs}`, { signal: ctl.signal });
          if (ctl.signal.aborted) return;
          all = all.concat(res.events);
          cursor = res.cursor;
          pages++;
          const capped = cursor !== null && all.length >= REPLAY_EVENTS_MAX;
          const done = cursor === null || capped;
          setState({ key: mine, s: { events: all, pages, done, capped, error: null } });
          if (capped) return;
        } while (cursor !== null);
      } catch {
        if (ctl.signal.aborted) return;
        setState({ key: mine, s: { events: all, pages, done: true, capped: false, error: "intervallo non leggibile, riprova" } });
      }
    })();
    return () => ctl.abort();
  }, [from, to]);
  return key !== null && state.key === key ? state.s : IDLE;
}
