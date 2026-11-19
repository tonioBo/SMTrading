import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { apiFetch } from "@/api/client";
import { instrumentsKey } from "@/api/hooks/markets";
import type { DayChanges, Facets, InstrumentBars, InstrumentMarket, InstrumentOverview, InstrumentSort, MarketSnapshot, QuickFilter, SearchResponse } from "@/api/marketsTypes";
import type { AssetClass, InstrumentsPage, QuotesResponse } from "@/api/types";

/** Rows per page of the «Strumenti» list: one batch quote request (at most 50) per page. */
export const LIST_PAGE = 50;
/** Prices of the visible page and the sector map are refreshed this often (no streaming). */
export const PAGE_QUOTES_MS = 60_000;
export const SNAPSHOT_MS = 5 * 60_000;

export interface ListFilter {
  query: string;
  asset_class: AssetClass | "";
  market: string;
  sector: string;
  index: string;
  quick: QuickFilter | "";
  sort: InstrumentSort;
  dir: "asc" | "desc";
  offset: number;
}

export function listParams(f: ListFilter): URLSearchParams {
  const p = new URLSearchParams({ offset: String(f.offset), limit: String(LIST_PAGE), sort: f.sort, dir: f.dir });
  for (const k of ["query", "asset_class", "market", "sector", "index", "quick"] as const) if (f[k]) p.set(k, f[k]);
  return p;
}

export function useInstrumentList(f: ListFilter) {
  const params = listParams(f);
  return useQuery({
    queryKey: [...instrumentsKey, "list", params.toString()],
    queryFn: () => apiFetch<InstrumentsPage>(`/api/instruments?${params.toString()}`),
    placeholderData: keepPreviousData,
  });
}

/** The ranked catalog search of the instant search box (none for an empty query). */
export function useRankedSearch(query: string, limit = 30) {
  const q = query.trim();
  return useQuery({
    queryKey: [...instrumentsKey, "ranked", q, limit],
    queryFn: ({ signal }) => apiFetch<SearchResponse>(`/api/instruments/search?${new URLSearchParams({ q, limit: String(limit) }).toString()}`, { signal }),
    enabled: q.length > 0,
    placeholderData: keepPreviousData,
    staleTime: 30_000,
  });
}

export function useFacets() {
  return useQuery({ queryKey: [...instrumentsKey, "facets"], queryFn: () => apiFetch<Facets>("/api/instruments/facets"), staleTime: 5 * 60_000 });
}

/** One batch quote request for the visible page, cached and refreshed every minute. */
export function usePageQuotes(symbols: string[]) {
  return useQuery({
    queryKey: ["page-quotes", symbols],
    queryFn: () => apiFetch<QuotesResponse>(`/api/quotes?symbols=${encodeURIComponent(symbols.join(","))}`),
    enabled: symbols.length > 0,
    staleTime: PAGE_QUOTES_MS,
    refetchInterval: PAGE_QUOTES_MS,
  });
}

/** Day changes of the visible page (batch, cached 5 minutes on the server). */
export function usePageDayChanges(symbols: string[]) {
  return useQuery({
    queryKey: ["day-changes", symbols],
    queryFn: () => apiFetch<DayChanges>(`/api/markets/day-changes?symbols=${encodeURIComponent(symbols.join(","))}`),
    enabled: symbols.length > 0,
    staleTime: PAGE_QUOTES_MS,
    refetchInterval: SNAPSHOT_MS,
  });
}

export function useInstrumentOverview(symbol: string | null) {
  return useQuery({
    queryKey: ["instrument-overview", symbol],
    queryFn: () => apiFetch<InstrumentOverview>(`/api/instruments/${encodeURIComponent(symbol!)}/overview`),
    enabled: !!symbol,
  });
}

export function useInstrumentMarket(symbol: string | null, timeframe: "1d" | "1h") {
  return useQuery({
    queryKey: ["instrument-market", symbol, timeframe],
    queryFn: () => apiFetch<InstrumentMarket>(`/api/instruments/${encodeURIComponent(symbol!)}/market?timeframe=${timeframe}`),
    enabled: !!symbol,
    staleTime: PAGE_QUOTES_MS,
    refetchInterval: PAGE_QUOTES_MS,
    placeholderData: keepPreviousData,
  });
}

/** The closed bars of a window (the «Diario operazioni» trade chart), cached a minute. */
export function useInstrumentBars(symbol: string | null, timeframe: "1d" | "1h", start: Date, end: Date) {
  // minute precision: an open trade's window (ending now) does not refetch on every render
  const iso = (d: Date) => new Date(Math.floor(d.getTime() / 60_000) * 60_000).toISOString();
  const params = new URLSearchParams({ timeframe, start: iso(start), end: iso(end) });
  return useQuery({
    queryKey: ["instrument-bars", symbol, params.toString()],
    queryFn: () => apiFetch<InstrumentBars>(`/api/instruments/${encodeURIComponent(symbol!)}/bars?${params.toString()}`),
    enabled: !!symbol,
    staleTime: PAGE_QUOTES_MS,
  });
}

/** Sector map and movers of a universe: only while shown, refreshed every 5 minutes. */
export function useMarketSnapshot(universe: string, enabled = true) {
  return useQuery({
    queryKey: ["market-snapshot", universe],
    queryFn: () => apiFetch<MarketSnapshot>(`/api/markets/snapshot?universe=${encodeURIComponent(universe)}`),
    enabled,
    staleTime: SNAPSHOT_MS,
    refetchInterval: SNAPSHOT_MS,
  });
}
