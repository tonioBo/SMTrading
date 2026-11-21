/** «Mercati v2» (plan 30): «Mercati oggi», favourites, screener, fundamentals, the week's
 * calendar, the news and the owner's personal price alerts. Portal only. */
import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetch } from "@/api/client";
import type {
  Favorites,
  Fundamentals,
  MarketNews,
  Overview,
  OverviewChartData,
  OverviewRange,
  PriceAlert,
  PriceAlertCreate,
  ScreenerPreset,
  ScreenerResult,
  WeekCalendar,
} from "@/api/marketsV2Types";

export const OVERVIEW_MS = 60_000;
export const favoritesKey = ["markets-favorites"] as const;
export const priceAlertsKey = ["markets-price-alerts"] as const;

/** «Mercati oggi»: cached 60 s on the server (5 min when the markets are closed). */
export function useOverview() {
  return useQuery({
    queryKey: ["markets-overview"],
    queryFn: () => apiFetch<Overview>("/api/markets/overview"),
    staleTime: OVERVIEW_MS,
    refetchInterval: OVERVIEW_MS,
  });
}

export function useOverviewChart(code: string | null, range: OverviewRange) {
  return useQuery({
    queryKey: ["markets-overview-chart", code, range],
    queryFn: () => apiFetch<OverviewChartData>(`/api/markets/overview/${encodeURIComponent(code!)}/chart?range=${range}`),
    enabled: !!code,
    staleTime: OVERVIEW_MS,
    placeholderData: keepPreviousData,
  });
}

export function useFavorites() {
  return useQuery({ queryKey: favoritesKey, queryFn: () => apiFetch<Favorites>("/api/markets/favorites"), staleTime: 30_000 });
}

export function useToggleFavorite() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ symbol, on }: { symbol: string; on: boolean }) =>
      apiFetch<Favorites>(`/api/markets/favorites/${encodeURIComponent(symbol)}`, { method: on ? "PUT" : "DELETE" }),
    onSuccess: (data) => qc.setQueryData(favoritesKey, data),
  });
}

export function useReorderFavorites() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (symbols: string[]) => apiFetch<Favorites>("/api/markets/favorites", { method: "PUT", body: { symbols } }),
    onSuccess: (data) => qc.setQueryData(favoritesKey, data),
  });
}

export function useScreener(preset: ScreenerPreset, universe: string, enabled = true) {
  const params = new URLSearchParams({ preset, universe });
  return useQuery({
    queryKey: ["markets-screener", params.toString()],
    queryFn: () => apiFetch<ScreenerResult>(`/api/markets/screener?${params.toString()}`),
    enabled,
    staleTime: 5 * 60_000,
    placeholderData: keepPreviousData,
  });
}

export function useFundamentals(symbol: string | null) {
  return useQuery({
    queryKey: ["instrument-fundamentals", symbol],
    queryFn: () => apiFetch<Fundamentals>(`/api/instruments/${encodeURIComponent(symbol!)}/fundamentals`),
    enabled: !!symbol,
    staleTime: 60 * 60_000,
  });
}

export function useWeekCalendar(enabled = true) {
  return useQuery({ queryKey: ["markets-calendar"], queryFn: () => apiFetch<WeekCalendar>("/api/markets/calendar"), enabled, staleTime: 30 * 60_000 });
}

export interface NewsFilter {
  symbol: string;
  sector: string;
  portfolios: boolean;
}

export function useMarketNews(f: NewsFilter, enabled = true) {
  const params = new URLSearchParams();
  if (f.symbol) params.set("symbol", f.symbol);
  else if (f.portfolios) params.set("portfolios", "true");
  else if (f.sector) params.set("sector", f.sector);
  return useQuery({
    queryKey: ["markets-news", params.toString()],
    queryFn: () => apiFetch<MarketNews>(`/api/markets/news?${params.toString()}`),
    enabled,
    staleTime: 5 * 60_000,
    refetchInterval: 10 * 60_000,
    placeholderData: keepPreviousData,
  });
}

export function usePriceAlerts(enabled = true) {
  return useQuery({ queryKey: priceAlertsKey, queryFn: () => apiFetch<PriceAlert[]>("/api/markets/price-alerts"), enabled, refetchInterval: 60_000 });
}

export function useCreatePriceAlert() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: PriceAlertCreate) => apiFetch<PriceAlert>("/api/markets/price-alerts", { method: "POST", body }),
    onSuccess: () => void qc.invalidateQueries({ queryKey: priceAlertsKey }),
  });
}

export function usePatchPriceAlert() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, ...body }: { id: number; threshold?: string; note?: string; direction?: string; rearm?: boolean }) =>
      apiFetch<PriceAlert>(`/api/markets/price-alerts/${id}`, { method: "PATCH", body }),
    onSuccess: () => void qc.invalidateQueries({ queryKey: priceAlertsKey }),
  });
}

export function useDeletePriceAlert() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => apiFetch<{ deleted: number }>(`/api/markets/price-alerts/${id}`, { method: "DELETE" }),
    onSuccess: () => void qc.invalidateQueries({ queryKey: priceAlertsKey }),
  });
}
