import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetch } from "@/api/client";
import type {
  AgentCostPeriod,
  AgentCosts,
  PaperclipConfig,
  PaperclipConfigPatch,
  PaperclipStatus,
  PaperclipVerifyResult,
} from "@/api/types";

export const paperclipConfigKey = ["paperclip", "config"] as const;
export const paperclipStatusKey = ["paperclip", "status"] as const;
export const agentCostsKey = ["agent-costs"] as const;

/** the reader runs every 5 minutes: the banner and the status line refresh every minute */
export const PAPERCLIP_POLL_MS = 60_000;

export function usePaperclipConfig() {
  return useQuery({ queryKey: paperclipConfigKey, queryFn: () => apiFetch<PaperclipConfig>("/api/paperclip/config") });
}

export function usePaperclipStatus() {
  return useQuery({
    queryKey: paperclipStatusKey,
    queryFn: () => apiFetch<PaperclipStatus>("/api/paperclip/status"),
    refetchInterval: PAPERCLIP_POLL_MS,
  });
}

/** Saves the changed fields; the variables may hold the board key, hence gcTime 0. */
export function useSavePaperclipConfig() {
  const qc = useQueryClient();
  return useMutation({
    gcTime: 0,
    mutationFn: (patch: PaperclipConfigPatch) => apiFetch<PaperclipConfig>("/api/paperclip/config", { method: "PUT", body: patch }),
    onSuccess: (data) => {
      qc.setQueryData(paperclipConfigKey, data);
      void qc.invalidateQueries({ queryKey: paperclipStatusKey });
      void qc.invalidateQueries({ queryKey: agentCostsKey });
    },
  });
}

/** «Verifica»: a test read with the saved configuration. */
export function useVerifyPaperclip() {
  return useMutation({
    mutationFn: () => apiFetch<PaperclipVerifyResult>("/api/paperclip/verify", { method: "POST" }),
  });
}

/** «Riconferma»: the current isolation rules become the expected ones. */
export function useReconfirmPaperclip() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => apiFetch<PaperclipStatus>("/api/paperclip/baseline", { method: "POST" }),
    onSuccess: (data) => qc.setQueryData(paperclipStatusKey, data),
  });
}

export function useAgentCosts(period: AgentCostPeriod) {
  return useQuery({
    queryKey: [...agentCostsKey, period],
    queryFn: () => apiFetch<AgentCosts>(`/api/agent-costs?period=${period}`),
    refetchInterval: PAPERCLIP_POLL_MS,
  });
}
