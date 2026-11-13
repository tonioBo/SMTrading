import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetch } from "@/api/client";
import type { FeeRule, FeeRuleInput, FeeSimulateRequest, FeeSimulation } from "@/api/types";

export const feeRulesKey = ["fee-rules"] as const;

export function useFeeRules() {
  return useQuery({ queryKey: feeRulesKey, queryFn: () => apiFetch<FeeRule[]>("/api/fee-rules") });
}

/** Create (no `id`) or update (PATCH with the full rule) a fee rule. */
export function useSaveFeeRule() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, rule }: { id: string | null; rule: FeeRuleInput }) =>
      id
        ? apiFetch<FeeRule>(`/api/fee-rules/${id}`, { method: "PATCH", body: rule })
        : apiFetch<FeeRule>("/api/fee-rules", { method: "POST", body: rule }),
    onSuccess: () => void qc.invalidateQueries({ queryKey: feeRulesKey }),
  });
}

/** Rules are never deleted: DELETE deactivates. */
export function useDeactivateFeeRule() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => apiFetch<FeeRule>(`/api/fee-rules/${id}`, { method: "DELETE" }),
    onSuccess: () => void qc.invalidateQueries({ queryKey: feeRulesKey }),
  });
}

export function useSimulateFee() {
  return useMutation({
    mutationFn: (body: FeeSimulateRequest) =>
      apiFetch<FeeSimulation>("/api/fee-rules/simulate", { method: "POST", body }),
  });
}
