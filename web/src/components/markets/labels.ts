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
