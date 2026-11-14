import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetch } from "@/api/client";
import { newKey } from "@/api/hooks/portfolios";
import type {
  AgentDocDetail,
  AgentDocInput,
  AgentDocSummary,
  AgentDocVersion,
  AgentDocVersionInfo,
} from "@/api/types";

export const agentDocsKey = ["agent-docs"] as const;

const path = (slug: string) => `/api/agent-docs/${encodeURIComponent(slug)}`;

export function useAgentDocs() {
  return useQuery({ queryKey: agentDocsKey, queryFn: () => apiFetch<AgentDocSummary[]>("/api/agent-docs") });
}

export function useAgentDoc(slug: string) {
  return useQuery({ queryKey: [...agentDocsKey, slug], queryFn: () => apiFetch<AgentDocDetail>(path(slug)) });
}

export function useAgentDocVersions(slug: string, enabled: boolean) {
  return useQuery({
    queryKey: [...agentDocsKey, slug, "versions"],
    queryFn: () => apiFetch<AgentDocVersionInfo[]>(`${path(slug)}/versions`),
    enabled,
  });
}

export function useAgentDocVersion(slug: string, version: number | null) {
  return useQuery({
    queryKey: [...agentDocsKey, slug, "versions", version],
    queryFn: () => apiFetch<AgentDocVersion>(`${path(slug)}/versions/${version}`),
    enabled: version !== null,
  });
}

export function useSaveAgentDoc(slug: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (input: AgentDocInput) =>
      apiFetch<{ doc: AgentDocDetail; changed: boolean }>(path(slug), {
        method: "PUT",
        body: input,
        idempotencyKey: newKey(),
      }),
    onSettled: () => void qc.invalidateQueries({ queryKey: agentDocsKey }),
  });
}

export function useRestoreAgentDoc(slug: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (version: number) =>
      apiFetch<{ doc: AgentDocDetail }>(`${path(slug)}/restore/${version}`, {
        method: "POST",
        idempotencyKey: newKey(),
      }),
    onSettled: () => void qc.invalidateQueries({ queryKey: agentDocsKey }),
  });
}
