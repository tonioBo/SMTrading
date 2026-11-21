/** «Mercati v2» (plan 30) API types: portal only. Numbers are decimal strings. */
import type { Instrument } from "@/api/types";

export interface OverviewItem {
  code: string;
  symbol: string;
  label: string;
  group: string;
  unit: string;
  decimals: number;
  note: string;
  currency: string | null;
  market: string;
  value: string | null;
  prev_close: string | null;
  change_pct: string | null;
  ts: string | null;
  is_open: boolean | null;
  next_open: string | null;
  next_close: string | null;
  /** [epoch seconds, value] */
  spark: [number, number][];
}

export interface Overview {
  as_of: string;
  ttl_s: number;
  stale: boolean;
  error?: string;
  items: OverviewItem[];
}

export type OverviewRange = "1d" | "5d" | "1mo" | "1y";

export interface OverviewChartData {
  code: string;
  range: OverviewRange;
  label: string;
  points: [number, number][];
  error: string | null;
}

export interface Favorite extends Instrument {
  position: number;
}

export interface Favorites {
  items: Favorite[];
}

export type ScreenerPreset = "rsi_oversold" | "rsi_overbought" | "above_sma200" | "below_sma200" | "new_high" | "new_low" | "volume_spike" | "near_low";

export interface ScreenerRow {
  symbol: string;
  name: string;
  sector: string | null;
  market: string;
  currency: string;
  asset_class: string;
  as_of: string;
  close: string;
  rsi14: string | null;
  sma200: string | null;
  vs_sma200_pct: string | null;
  high_52w: string | null;
  low_52w: string | null;
  from_low_pct: string | null;
  from_high_pct: string | null;
  volume_ratio: string | null;
  chg_1d: string | null;
  chg_1w: string | null;
  chg_1m: string | null;
  chg_ytd: string | null;
}

export interface ScreenerResult {
  preset: ScreenerPreset;
  label: string;
  presets: { code: ScreenerPreset; label: string }[];
  universe: string;
  universes: { code: string; label: string }[];
  items: ScreenerRow[];
  matched: number;
  computed: number;
  total: number;
  as_of: string | null;
}

export interface Fundamentals {
  symbol: string;
  market_cap: string | null;
  pe_trailing: string | null;
  pe_forward: string | null;
  dividend_yield: string | null;
  low_52w: string | null;
  high_52w: string | null;
  beta: string | null;
  next_earnings: string | null;
  earnings_estimate: boolean | null;
  ex_dividend: string | null;
  dividend_date: string | null;
  currency: string | null;
  source: string | null;
}

export type CalendarKind = "earnings" | "ex_dividend" | "macro";

export interface CalendarEvent {
  date: string;
  kind: CalendarKind;
  symbol: string | null;
  name: string;
  sector: string | null;
  held: boolean;
  portfolios: string[];
  estimate: boolean;
  label: string | null;
}

export interface WeekCalendar {
  from: string;
  to: string;
  today: string;
  events: CalendarEvent[];
  covered: number;
  total: number;
  macro: { as_of: string; sources: Record<string, string>; note: string };
}

export interface NewsRow {
  id: string;
  ts: string;
  headline: string;
  summary: string | null;
  url: string | null;
  source: string;
  symbols: string[];
}

export interface MarketNews {
  symbols: string[];
  items: NewsRow[];
  error: string | null;
}

export type AlertDirection = "above" | "below";
export type AlertSource = "catalog" | "overview";

export interface PriceAlert {
  id: number;
  symbol: string;
  label: string;
  source: AlertSource;
  direction: AlertDirection;
  threshold: string;
  note: string;
  status: "active" | "triggered";
  created_at: string;
  triggered_at: string | null;
  triggered_price: string | null;
}

export interface PriceAlertCreate {
  symbol: string;
  source: AlertSource;
  direction: AlertDirection;
  threshold: string;
  note: string;
}

export type SnapshotPeriod = "1d" | "1w" | "1m" | "ytd";
