import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { apiFetch } from "@/api/client";
import type { TradeDetail, TradeFilters, TradesResponse } from "@/api/types";

/** the list is cached a minute on the server */
export const TRADES_REFRESH_MS = 60_000;
export const TRADES_PAGE_SIZE = 50;

export function tradesQuery(f: TradeFilters): string {
  const q = new URLSearchParams({ period: f.period, page: String(f.page), page_size: String(TRADES_PAGE_SIZE) });
  if (f.portfolio_id) q.set("portfolio_id", f.portfolio_id);
  if (f.strategy) q.set("strategy", f.strategy);
  if (f.symbol) q.set("symbol", f.symbol);
  if (f.outcome) q.set("outcome", f.outcome);
  return q.toString();
}

export function useTrades(f: TradeFilters) {
  const qs = tradesQuery(f);
  return useQuery({
    queryKey: ["trades", qs],
    queryFn: () => apiFetch<TradesResponse>(`/api/trades?${qs}`),
    refetchInterval: TRADES_REFRESH_MS,
    refetchIntervalInBackground: false,
    staleTime: TRADES_REFRESH_MS,
    placeholderData: keepPreviousData,
  });
}

export function useTrade(key: string) {
  return useQuery({
    queryKey: ["trades", "detail", key],
    queryFn: () => apiFetch<TradeDetail>(`/api/trades/${encodeURIComponent(key)}`),
    staleTime: TRADES_REFRESH_MS,
  });
}
