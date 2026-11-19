import type { TradeOutcome, TradePeriod } from "@/api/types";

export const NO_STRATEGY = "Senza strategia";

export const PERIOD_LABELS: Record<TradePeriod, string> = {
  week: "Questa settimana",
  "1m": "Ultimi 30 giorni",
  "3m": "Ultimi 90 giorni",
  all: "Tutto",
};

export const OUTCOME_LABELS: Record<TradeOutcome, string> = { win: "Vincente", loss: "Perdente", open: "Aperta" };

/** Link to the «Diario operazioni» with some filters set (from «Posizioni», «Risultati agenti»…). */
export function journalLink(f: Partial<Record<"portfolio_id" | "strategy" | "symbol" | "outcome", string>>): string {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(f)) if (v) q.set(k, v);
  const s = q.toString();
  return s ? `/diario-operazioni?${s}` : "/diario-operazioni";
}
