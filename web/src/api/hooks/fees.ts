import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { apiFetch } from "@/api/client";
import { errorMessage } from "@/lib/errors";
import type {
  BrokerFeePreset,
  FeePresetApplyResult,
  FeeRule,
  FeeRuleInput,
  FeeRuleSimulateRequest,
  FeeRuleSimulation,
} from "@/api/types";

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

export function useSimulateFeeRule() {
  return useMutation({
    mutationFn: (body: FeeRuleSimulateRequest) =>
      apiFetch<FeeRuleSimulation>("/api/fees/simulate", { method: "POST", body }),
  });
}

export const feePresetsKey = ["fee-presets"] as const;

export function useBrokerFeePresets() {
  return useQuery({
    queryKey: feePresetsKey,
    queryFn: () => apiFetch<BrokerFeePreset[]>("/api/brokers/fee-presets"),
    staleTime: Infinity, // a fixed backend table
  });
}

/** The preset of `kind` (undefined while loading or for a broker without one). */
export function useBrokerFeePreset(kind: string | null | undefined): BrokerFeePreset | undefined {
  const { data } = useBrokerFeePresets();
  return kind ? data?.find((p) => p.broker_kind === kind) : undefined;
}

/** The broker's preset becomes the portfolio's fee rule «<portafoglio> – <broker>». */
export function useApplyFeePreset() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ portfolioId, brokerKind }: { portfolioId: string; brokerKind: string }) =>
      apiFetch<FeePresetApplyResult>(`/api/portfolios/${portfolioId}/fee-rule/apply-preset`, {
        method: "POST",
        body: { broker_kind: brokerKind },
      }),
    onSuccess: () => void qc.invalidateQueries({ queryKey: feeRulesKey }),
  });
}

/** After a successful link: applies the preset (a separate request; the link stays either
 * way). The toasts are on the mutation itself: they show even once the link dialog closed. */
export function useApplyPresetAfterLink() {
  const qc = useQueryClient();
  const apply = useMutation({
    mutationFn: ({ portfolioId, preset }: { portfolioId: string; preset: BrokerFeePreset }) =>
      apiFetch<FeePresetApplyResult>(`/api/portfolios/${portfolioId}/fee-rule/apply-preset`, {
        method: "POST",
        body: { broker_kind: preset.broker_kind },
      }),
    onSuccess: (_res, { preset }) => {
      void qc.invalidateQueries({ queryKey: feeRulesKey });
      toast.success(`Commissioni di ${preset.broker} applicate al portafoglio`);
    },
    onError: (err) => toast.error(`Portafoglio collegato, ma commissioni non applicate: ${errorMessage(err)}`),
  });
  return (portfolioId: string, preset: BrokerFeePreset) => apply.mutate({ portfolioId, preset });
}
