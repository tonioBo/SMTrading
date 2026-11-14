import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetch } from "@/api/client";
import type { TradingHalt, TradingHaltInput, TradingHalts } from "@/api/types";
import { useLiveMessage } from "@/lib/ws";

export const haltsKey = ["halts"] as const;

/** Active halts and history, refetched on the `halt.set` / `halt.cleared` live events. */
export function useHalts() {
  const qc = useQueryClient();
  useLiveMessage("event", (event) => {
    if (event.type === "halt.set" || event.type === "halt.cleared") {
      void qc.invalidateQueries({ queryKey: haltsKey });
    }
  });
  return useQuery({ queryKey: haltsKey, queryFn: () => apiFetch<TradingHalts>("/api/halts") });
}

/** Active halts covering a portfolio (global ones included); every active halt without id. */
export function coveringHalts(halts: TradingHalt[] | undefined, portfolioId?: string): TradingHalt[] {
  if (!halts) return [];
  if (!portfolioId) return halts;
  return halts.filter((h) => h.scope === "global" || h.portfolio_id === portfolioId);
}

export function useSetHalt() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (input: TradingHaltInput) =>
      apiFetch<{ halt: TradingHalt; changed: boolean; canceled: number }>("/api/halts", { method: "POST", body: input }),
    onSettled: () => void qc.invalidateQueries({ queryKey: haltsKey }),
  });
}

export function useClearHalt() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: number) =>
      apiFetch<{ halt: TradingHalt; changed: boolean }>(`/api/halts/${id}/clear`, { method: "POST" }),
    onSettled: () => void qc.invalidateQueries({ queryKey: haltsKey }),
  });
}
