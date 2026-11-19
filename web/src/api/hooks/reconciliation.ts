import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetch } from "@/api/client";
import type { ReconciliationDetail, ReconciliationPortfolio } from "@/api/types";

export const reconciliationKey = ["reconciliation"] as const;
export const reconciliationDetailKey = (portfolioId: string) => ["reconciliation", portfolioId] as const;

export const HISTORY_LIMIT = 20;

/** Every portfolio with its last check against the broker (not linked ones too). */
export function useReconciliation() {
  return useQuery({
    queryKey: reconciliationKey,
    queryFn: () => apiFetch<{ portfolios: ReconciliationPortfolio[] }>("/api/reconciliation").then((r) => r.portfolios),
    refetchInterval: 60_000,
  });
}

/** One portfolio: the last check with its items, and the history. */
export function useReconciliationDetail(portfolioId: string | null) {
  return useQuery({
    queryKey: reconciliationDetailKey(portfolioId ?? ""),
    queryFn: () => apiFetch<ReconciliationDetail>(`/api/reconciliation/${portfolioId}?limit=${HISTORY_LIMIT}`),
    enabled: portfolioId != null,
  });
}

/** «Riconcilia ora»: one portfolio's broker link, or every linked portfolio (read-only at the broker). */
export function useRunReconciliation() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (portfolioId: string | null) =>
      apiFetch<{ portfolios: ReconciliationPortfolio[] }>("/api/reconciliation/run", {
        method: "POST",
        body: portfolioId ? { portfolio_id: portfolioId } : {},
      }).then((r) => r.portfolios),
    onSuccess: (rows) => {
      qc.setQueryData(reconciliationKey, rows);
      void qc.invalidateQueries({ queryKey: reconciliationKey });
    },
  });
}
