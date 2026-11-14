import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetch } from "@/api/client";
import { streamStatusKey } from "@/api/hooks/connectors";
import type {
  Agent,
  AgentAlert,
  AgentCreated,
  AgentPortfolioAssignment,
  AgentRotated,
  AgentWatchlist,
  McpAuditEntry,
  McpAuditFilters,
} from "@/api/types";

export const agentsKey = ["agents"] as const;

export function useAgents() {
  return useQuery({ queryKey: agentsKey, queryFn: () => apiFetch<Agent[]>("/api/agents") });
}

/**
 * Token-bearing mutations use gcTime 0: the mutation (and its result) leaves the React Query
 * cache as soon as the dialog showing the token unmounts. The token itself is never stored.
 */
export function useCreateAgent() {
  const qc = useQueryClient();
  return useMutation({
    gcTime: 0,
    mutationFn: (name: string) => apiFetch<AgentCreated>("/api/agents", { method: "POST", body: { name } }),
    onSuccess: () => void qc.invalidateQueries({ queryKey: agentsKey }),
  });
}

export function useRotateAgentToken() {
  return useMutation({
    gcTime: 0,
    mutationFn: (agentId: string) => apiFetch<AgentRotated>(`/api/agents/${agentId}/rotate`, { method: "POST" }),
  });
}

export function useSetAgentActive() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, active }: { id: string; active: boolean }) =>
      apiFetch<Agent>(`/api/agents/${id}`, { method: "PATCH", body: { active } }),
    onSuccess: () => void qc.invalidateQueries({ queryKey: agentsKey }),
  });
}

/** The `notify` permission: the agent may send messages to the Telegram group. */
export function useSetAgentNotify() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, can_notify }: { id: string; can_notify: boolean }) =>
      apiFetch<Agent>(`/api/agents/${id}`, { method: "PATCH", body: { can_notify } }),
    onSuccess: () => void qc.invalidateQueries({ queryKey: agentsKey }),
  });
}

/** The `halt` permission: the agent may block new openings (set_trading_halt). */
export function useSetAgentHalt() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, can_halt }: { id: string; can_halt: boolean }) =>
      apiFetch<Agent>(`/api/agents/${id}`, { method: "PATCH", body: { can_halt } }),
    onSuccess: () => void qc.invalidateQueries({ queryKey: agentsKey }),
  });
}

/** Replaces the agent's whole set of portfolio assignments. */
export function useSetAgentPortfolios(agentId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (assignments: AgentPortfolioAssignment[]) =>
      apiFetch<Agent>(`/api/agents/${agentId}/portfolios`, { method: "PUT", body: assignments }),
    onSuccess: () => void qc.invalidateQueries({ queryKey: agentsKey }),
  });
}

export function useMcpAudit(filters: McpAuditFilters) {
  const params = new URLSearchParams();
  if (filters.agent_id) params.set("agent_id", filters.agent_id);
  if (filters.method.trim()) params.set("method", filters.method.trim());
  if (filters.outcome.trim()) params.set("outcome", filters.outcome.trim());
  if (filters.since) params.set("since", new Date(`${filters.since}T00:00:00`).toISOString());
  const qs = params.toString();
  return useQuery({
    queryKey: ["mcp-audit", filters],
    queryFn: () => apiFetch<McpAuditEntry[]>(`/api/mcp-audit${qs ? `?${qs}` : ""}`),
  });
}

// --- watchlists and alerts of an agent ------------------------------------------------

export const agentWatchlistsKey = (agentId: string) => ["agents", agentId, "watchlists"] as const;
export const agentAlertsKey = (agentId: string) => ["agents", agentId, "alerts"] as const;

export function useAgentWatchlists(agentId: string) {
  return useQuery({
    queryKey: agentWatchlistsKey(agentId),
    queryFn: () => apiFetch<AgentWatchlist[]>(`/api/agents/${agentId}/watchlists`),
  });
}

export function useAgentAlerts(agentId: string) {
  return useQuery({
    queryKey: agentAlertsKey(agentId),
    queryFn: () => apiFetch<AgentAlert[]>(`/api/agents/${agentId}/alerts`),
  });
}

/** After a change: the agent's lists and the Mercati tracking («Seguito da»). */
function useWatchChanged(agentId: string) {
  const qc = useQueryClient();
  return () => {
    void qc.invalidateQueries({ queryKey: agentWatchlistsKey(agentId) });
    void qc.invalidateQueries({ queryKey: agentAlertsKey(agentId) });
    void qc.invalidateQueries({ queryKey: streamStatusKey });
  };
}

export function useRemoveWatchSymbol(agentId: string) {
  const changed = useWatchChanged(agentId);
  return useMutation({
    mutationFn: ({ watchlistId, symbol }: { watchlistId: string; symbol: string }) =>
      apiFetch<AgentWatchlist>(`/api/agents/${agentId}/watchlists/${watchlistId}/items/${encodeURIComponent(symbol)}`, {
        method: "DELETE",
      }),
    onSettled: changed,
  });
}

export function useDeleteWatchlist(agentId: string) {
  const changed = useWatchChanged(agentId);
  return useMutation({
    mutationFn: (watchlistId: string) => apiFetch(`/api/agents/${agentId}/watchlists/${watchlistId}`, { method: "DELETE" }),
    onSettled: changed,
  });
}

export function useCancelAlert(agentId: string) {
  const changed = useWatchChanged(agentId);
  return useMutation({
    mutationFn: (alertId: string) => apiFetch<AgentAlert>(`/api/agents/${agentId}/alerts/${alertId}/cancel`, { method: "POST" }),
    onSettled: changed,
  });
}
