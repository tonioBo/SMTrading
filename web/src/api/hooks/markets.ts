import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetch } from "@/api/client";
import { streamStatusKey } from "@/api/hooks/connectors";
import type { AutoAddSetting, Instrument, InstrumentCreate, InstrumentsPage, MarketClosure, MarketClosureCreate, MarketStatus, QuotesResponse, UntrackResponse } from "@/api/types";

export const instrumentsKey = ["instruments"] as const;
export const marketsStatusKey = ["markets-status"] as const;
export const autoAddKey = ["instruments-auto-add"] as const;
export const marketClosuresKey = ["market-closures"] as const;
export const INSTRUMENTS_PAGE = 100;

export interface InstrumentFilter {
  query: string;
  market: string;
  offset: number;
}

export function useInstruments({ query, market, offset }: InstrumentFilter) {
  const params = new URLSearchParams({ query, offset: String(offset), limit: String(INSTRUMENTS_PAGE) });
  if (market) params.set("market", market);
  return useQuery({
    queryKey: [...instrumentsKey, query, market, offset],
    queryFn: () => apiFetch<InstrumentsPage>(`/api/instruments?${params.toString()}`),
    placeholderData: keepPreviousData,
  });
}

export function useAutoAdd() {
  return useQuery({ queryKey: autoAddKey, queryFn: () => apiFetch<AutoAddSetting>("/api/instruments/auto-add") });
}

export function useSetAutoAdd() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (on: boolean) => apiFetch<AutoAddSetting>("/api/instruments/auto-add", { method: "PUT", body: { on } }),
    onSuccess: (data) => qc.setQueryData(autoAddKey, data),
  });
}

export function useCreateInstrument() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: InstrumentCreate) => apiFetch<Instrument>("/api/instruments", { method: "POST", body }),
    onSuccess: () => void qc.invalidateQueries({ queryKey: instrumentsKey }),
  });
}

export function useMarketsStatus() {
  return useQuery({
    queryKey: marketsStatusKey,
    queryFn: () => apiFetch<MarketStatus[]>("/api/markets/status"),
    refetchInterval: 60_000,
  });
}

export function useMarketClosures() {
  return useQuery({ queryKey: marketClosuresKey, queryFn: () => apiFetch<MarketClosure[]>("/api/market-closures") });
}

/** A manual closure changes the market hours at once: both lists are reloaded. */
function useClosureMutation<T>(mutationFn: (arg: T) => Promise<unknown>) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn,
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: marketClosuresKey });
      void qc.invalidateQueries({ queryKey: marketsStatusKey });
    },
  });
}

export function useAddMarketClosure() {
  return useClosureMutation((body: MarketClosureCreate) => apiFetch<MarketClosure>("/api/market-closures", { method: "POST", body }));
}

export function useDeleteMarketClosure() {
  return useClosureMutation((id: number) => apiFetch<{ deleted: number }>(`/api/market-closures/${id}`, { method: "DELETE" }));
}

export function useTrack() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ symbol, track }: { symbol: string; track: boolean }) =>
      apiFetch<Partial<UntrackResponse>>(`/api/marketdata/${track ? "track" : "untrack"}`, { method: "POST", body: { symbols: [symbol] } }),
    onSettled: () => void qc.invalidateQueries({ queryKey: streamStatusKey }),
  });
}

/** Snapshot quotes (at most 50 symbols) to fill the grid before the first live tick. */
export function useQuotes(symbols: string[]) {
  return useQuery({
    queryKey: ["quotes", symbols],
    queryFn: () => apiFetch<QuotesResponse>(`/api/quotes?symbols=${encodeURIComponent(symbols.join(","))}`),
    enabled: symbols.length > 0,
  });
}
