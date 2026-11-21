/** The «Mercati» page's filters, sort and views, remembered per browser (localStorage,
 * every access wrapped: blocked storage just means defaults). */
import { useCallback, useState } from "react";
import type { InstrumentSort, QuickFilter } from "@/api/marketsTypes";
import type { AssetClass } from "@/api/types";

export const MARKETS_PREFS_KEY = "smt-markets-prefs-v1";

export interface MarketsPrefs {
  asset_class: AssetClass | "";
  market: string;
  sector: string;
  index: string;
  quick: QuickFilter | "";
  /** a server sort, or "change": the day change, sorted within the page */
  sort: InstrumentSort | "change";
  dir: "asc" | "desc";
  /** universe of the sector map and the movers */
  universe: string;
  /** sector map / movers panel shown */
  mapOpen: boolean;
  /** the tab of «I miei strumenti» (plan 30) */
  tab: MarketsTab;
}

export type MarketsTab = "favorites" | "screener" | "calendar" | "news" | "alerts";
export const MARKETS_TABS: MarketsTab[] = ["favorites", "screener", "calendar", "news", "alerts"];

export const DEFAULT_PREFS: MarketsPrefs = {
  asset_class: "",
  market: "",
  sector: "",
  index: "",
  quick: "",
  sort: "symbol",
  dir: "asc",
  universe: "SP500",
  mapOpen: true,
  tab: "favorites",
};

const SORTS: (InstrumentSort | "change")[] = ["symbol", "name", "sector", "market", "asset_class", "created_at", "change"];
const QUICK: (QuickFilter | "")[] = ["", "portfolio", "watchlist", "strategy", "signal", "recent"];
const CLASSES: (AssetClass | "")[] = ["", "us_equity", "eu_equity", "crypto", "forex"];

/** Stored values, each checked: anything unexpected falls back to its default. */
export function readPrefs(): MarketsPrefs {
  let raw: unknown = null;
  try {
    raw = JSON.parse(localStorage.getItem(MARKETS_PREFS_KEY) ?? "null");
  } catch {
    return DEFAULT_PREFS;
  }
  if (!raw || typeof raw !== "object") return DEFAULT_PREFS;
  const r = raw as Record<string, unknown>;
  const str = (k: keyof MarketsPrefs) => (typeof r[k] === "string" ? (r[k] as string) : (DEFAULT_PREFS[k] as string));
  return {
    asset_class: CLASSES.includes(r.asset_class as AssetClass) ? (r.asset_class as AssetClass | "") : "",
    market: str("market"),
    sector: str("sector"),
    index: str("index"),
    quick: QUICK.includes(r.quick as QuickFilter) ? (r.quick as QuickFilter | "") : "",
    sort: SORTS.includes(r.sort as InstrumentSort) ? (r.sort as InstrumentSort) : "symbol",
    dir: r.dir === "desc" ? "desc" : "asc",
    universe: str("universe"),
    mapOpen: typeof r.mapOpen === "boolean" ? r.mapOpen : true,
    tab: MARKETS_TABS.includes(r.tab as MarketsTab) ? (r.tab as MarketsTab) : "favorites",
  };
}

export function writePrefs(p: MarketsPrefs): void {
  try {
    localStorage.setItem(MARKETS_PREFS_KEY, JSON.stringify(p));
  } catch {
    // not persisted: the choice still applies to this page
  }
}

export function useMarketsPrefs(): [MarketsPrefs, (patch: Partial<MarketsPrefs>) => void] {
  const [prefs, setPrefs] = useState(readPrefs);
  const update = useCallback((patch: Partial<MarketsPrefs>) => {
    setPrefs((prev) => {
      const next = { ...prev, ...patch };
      writePrefs(next);
      return next;
    });
  }, []);
  return [prefs, update];
}

/** `text` split around the case-insensitive occurrences of `query` (for <mark>). */
export function highlightParts(text: string, query: string): { text: string; hit: boolean }[] {
  const q = query.trim().toLowerCase();
  if (!q) return [{ text, hit: false }];
  const out: { text: string; hit: boolean }[] = [];
  const lower = text.toLowerCase();
  let i = 0;
  while (i < text.length) {
    const j = lower.indexOf(q, i);
    if (j < 0) break;
    if (j > i) out.push({ text: text.slice(i, j), hit: false });
    out.push({ text: text.slice(j, j + q.length), hit: true });
    i = j + q.length;
  }
  if (i < text.length) out.push({ text: text.slice(i), hit: false });
  return out.length ? out : [{ text, hit: false }];
}
