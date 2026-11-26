import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetch } from "@/api/client";
import type { IntradayAccount, IntradayTrigger } from "@/api/types";

export const intradayTriggersKey = (portfolioId?: string) => ["intraday", "triggers", portfolioId ?? "all"] as const;
export const intradayAccountsKey = ["intraday", "accounts"] as const;
/** the trigger log and the account state refresh once a minute while the tab is visible */
export const INTRADAY_REFRESH_MS = 60_000;

/** The latest triggers (newest first), of one portfolio or of all. */
export function useIntradayTriggers(portfolioId?: string) {
  return useQuery({
    queryKey: intradayTriggersKey(portfolioId),
    queryFn: () => apiFetch<IntradayTrigger[]>(portfolioId ? `/api/intraday/triggers?portfolio_id=${encodeURIComponent(portfolioId)}` : "/api/intraday/triggers"),
    refetchInterval: INTRADAY_REFRESH_MS,
    refetchIntervalInBackground: false,
  });
}

/** The broker account state per link, with the day-trade usage of its portfolios. */
export function useIntradayAccounts() {
  return useQuery({
    queryKey: intradayAccountsKey,
    queryFn: () => apiFetch<IntradayAccount[]>("/api/intraday/accounts"),
    refetchInterval: INTRADAY_REFRESH_MS,
    refetchIntervalInBackground: false,
  });
}

/** End-of-drill cleanup: deletes every trigger of the portfolio (admin). */
export function useDeleteIntradayTriggers(portfolioId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => apiFetch<{ deleted: number }>(`/api/portfolios/${portfolioId}/intraday-triggers`, { method: "DELETE" }),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["intraday", "triggers"] }),
  });
}
