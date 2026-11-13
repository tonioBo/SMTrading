import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetch } from "@/api/client";
import { streamStatusKey } from "@/api/hooks/connectors";
import type { Instrument, InstrumentCreate, MarketStatus, QuotesResponse } from "@/api/types";

export const instrumentsKey = ["instruments"] as const;
export const marketsStatusKey = ["markets-status"] as const;

export function useInstruments(query: string) {
  return useQuery({
    queryKey: [...instrumentsKey, query],
    queryFn: () => apiFetch<Instrument[]>(`/api/instruments?query=${encodeURIComponent(query)}`),
    placeholderData: keepPreviousData,
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

export function useTrack() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ symbol, track }: { symbol: string; track: boolean }) =>
      apiFetch(`/api/marketdata/${track ? "track" : "untrack"}`, { method: "POST", body: { symbols: [symbol] } }),
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
