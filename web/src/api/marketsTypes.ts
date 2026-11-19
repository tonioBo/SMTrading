/** Types of the «Mercati» page (plan 22): search, facets, instrument sheet, sector map. */
import type { Instrument, Quote } from "@/api/types";

export type SearchMatch = "symbol" | "name" | "index" | "sector" | "industry";

export interface SearchHit extends Instrument {
  score: number;
  match: SearchMatch;
}

export interface SearchResponse {
  query: string;
  items: SearchHit[];
}

export interface Facets {
  sectors: { sector: string; count: number }[];
  indices: { code: string; label: string; count: number }[];
  universes: { code: string; label: string }[];
  sector_list: string[];
  etf_types: string[];
}

export type QuickFilter = "portfolio" | "watchlist" | "strategy" | "signal" | "recent";
export type InstrumentSort = "symbol" | "name" | "sector" | "market" | "asset_class" | "created_at";

export interface InstrumentOverview {
  instrument: Instrument;
  market_status: { market: string; is_open: boolean; next_open: string | null; next_close: string | null } | null;
  positions: { portfolio_id: string; portfolio_name: string; qty: string; avg_price: string; opened_at: string | null }[];
  open_orders: {
    id: string;
    portfolio_id: string;
    portfolio_name: string;
    side: "buy" | "sell";
    type: string;
    qty: string | null;
    notional: string | null;
    limit_price: string | null;
    stop_price: string | null;
    role: string | null;
    status: string;
    created_at: string;
  }[];
  watchlists: { agent: string; watchlist: string }[];
  strategies: { ref: string; name: string; type: string | null; timeframe: string | null; portfolios: string[] }[];
  decisions: { id: number; kind: string; summary: string; created_at: string }[];
  /** from the cached «Segnali» answer; null when there is none yet */
  signals:
    | {
        ref: string;
        name: string;
        portfolio_name: string | null;
        indicator: string | null;
        direction: string | null;
        threshold: string | null;
        distance: string | null;
        triggered_now: boolean | null;
        position: { qty: string; avg_price: string } | null;
        as_of: string | null;
      }[]
    | null;
}

export interface DayChange {
  last: string;
  prev_close: string;
  change_pct: string;
  ts: string;
  provider: string;
}

export interface InstrumentMarket {
  symbol: string;
  timeframe: "1d" | "1h";
  quote: Quote | null;
  day_change: DayChange | null;
  bars: { ts: string; close: string }[];
  indicators: { close: string; ts: string; rsi14: string | null; sma20: string | null; sma50: string | null; atr14: string | null } | null;
  news: { id: string; ts: string; headline: string; url: string | null; source: string }[];
  next_earnings: { date: string; data: Record<string, unknown> } | null;
  next_dividend: { date: string; data: Record<string, unknown> } | null;
  errors: Record<string, string>;
  as_of: string;
}

export interface Mover {
  symbol: string;
  name: string;
  sector: string;
  currency: string;
  last: string;
  change_pct: string;
}

export interface SectorTile {
  sector: string;
  count: number;
  avg_change_pct: string;
  up: number;
  down: number;
  members: Mover[];
}

export interface MarketSnapshot {
  universe: string;
  label: string;
  as_of: string;
  total: number;
  covered: number;
  sectors: SectorTile[];
  gainers: Mover[];
  losers: Mover[];
}

export type DayChanges = Record<string, DayChange>;
