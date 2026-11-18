import { useQuery } from "@tanstack/react-query";
import { apiFetch } from "@/api/client";
import type { AgentResults, AgentResultsPeriod } from "@/api/types";

export const agentResultsKey = (period: AgentResultsPeriod) => ["agent-results", period] as const;
/** the answer is cached a minute on the server */
export const AGENT_RESULTS_REFRESH_MS = 60_000;

export function useAgentResults(period: AgentResultsPeriod) {
  return useQuery({
    queryKey: agentResultsKey(period),
    queryFn: () => apiFetch<AgentResults>(`/api/agent-results?period=${period}`),
    refetchInterval: AGENT_RESULTS_REFRESH_MS,
    refetchIntervalInBackground: false,
    staleTime: AGENT_RESULTS_REFRESH_MS,
  });
}
