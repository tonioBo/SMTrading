import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetch } from "@/api/client";
import type {
  Agent,
  AgentCreated,
  AgentPortfolioAssignment,
  AgentRotated,
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
