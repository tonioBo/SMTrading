/** «Salute del sistema»: labels and tones of the statuses and the worker jobs. */
import type { HealthStatus } from "@/api/types";
import type { StatusTone } from "@/components/StatusBadge";

export const HEALTH_TONE: Record<HealthStatus, StatusTone> = {
  ok: "ok",
  attenzione: "warn",
  problema: "danger",
  spento: "neutral",
};

export const HEALTH_LABEL: Record<HealthStatus, string> = {
  ok: "Tutto ok",
  attenzione: "Attenzione",
  problema: "Problema",
  spento: "Spento",
};

/** The worker jobs in plain Italian (the backend names are the log names). */
export const JOB_LABELS: Record<string, string> = {
  "market closures": "Chiusure dei mercati",
  "stream manager": "Quotazioni in tempo reale",
  "health checks": "Controllo fornitori di dati",
  "order processor": "Esecuzione ordini",
  "alert evaluator": "Alert sui prezzi",
  "backtest runner": "Backtest",
  "broker sync": "Sincronizzazione broker",
  "live monitor": "Controllo denaro reale",
  "job scheduler": "Lavori pianificati",
  "telegram planner": "Telegram: preparazione",
  "telegram sender": "Telegram: invio",
  "telegram summaries": "Telegram: riepiloghi",
  "paperclip reader": "Lettura di Paperclip",
  "protection alarm": "Allarme protezioni",
  "goal alerts": "Avvisi obiettivi",
  "plan closer": "Chiusura piani",
  "pre-open check": "Controllo pre-apertura",
  "hand-off watchdog": "Controllo passaggi agenti",
  "price quality alarm": "Allarme prezzi non affidabili",
  "broker tradability": "Strumenti presso il broker",
};

export const ASSET_CLASS_LABELS: Record<string, string> = {
  us_equity: "Azioni USA",
  eu_equity: "Azioni Europa",
  crypto: "Cripto",
  forex: "Valute",
};

/** Seconds as «45 s», «12 min», «3,5 h». */
export function formatAge(seconds: number | null | undefined): string {
  if (seconds == null) return "—";
  const s = Math.max(0, Math.round(seconds));
  if (s < 90) return `${s} s`;
  if (s < 90 * 60) return `${Math.round(s / 60)} min`;
  return `${(s / 3600).toFixed(1).replace(".", ",")} h`;
}
