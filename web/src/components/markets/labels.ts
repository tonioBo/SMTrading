/** Labels of the «Mercati» page. */

export const INDEX_LABELS: Record<string, string> = {
  SP500: "S&P 500",
  NDX: "NASDAQ-100",
  FTSEMIB: "FTSE MIB",
  DAX: "DAX 40",
  CAC40: "CAC 40",
  MDAX: "MDAX",
};

export const QUICK_LABELS = {
  portfolio: "In portafoglio",
  watchlist: "Nelle watchlist degli agenti",
  strategy: "Usati da strategie",
  signal: "Con segnale attivo",
  recent: "Aggiunti di recente",
} as const;

export const UNSECTORED = "Senza settore";

export const DEFAULT_UNIVERSES = [
  { code: "SP500", label: "S&P 500" },
  { code: "NDX", label: "NASDAQ-100" },
  { code: "FTSEMIB", label: "FTSE MIB" },
  { code: "DAX", label: "DAX 40" },
  { code: "CAC40", label: "CAC 40" },
  { code: "MDAX", label: "MDAX" },
  { code: "CRYPTO", label: "Crypto" },
  { code: "MINE", label: "Watchlist e posizioni" },
];

/** ±3% or more is the full colour; the sign picks green or red. */
const FULL_SCALE = 3;

export function tileStyle(avg: string): { backgroundColor: string } {
  const v = Number(avg);
  if (!Number.isFinite(v) || v === 0) return { backgroundColor: "rgb(var(--border) / 0.5)" };
  const alpha = 0.15 + 0.65 * Math.min(Math.abs(v) / FULL_SCALE, 1);
  return { backgroundColor: `rgb(var(${v > 0 ? "--gain" : "--loss"}) / ${alpha.toFixed(2)})` };
}
