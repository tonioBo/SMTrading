/** Labels of the «Obiettivi» (goals page, dashboard card, «Andamento» lines). */
import type { Goal, GoalHorizon, GoalKind, GoalProgress, GoalStatus } from "@/api/types";
import type { StatusTone } from "@/components/StatusBadge";
import { formatMoney, formatPct } from "@/lib/format";

export const HORIZON_OPTIONS: { value: GoalHorizon; label: string }[] = [
  { value: "week", label: "Settimana" },
  { value: "month", label: "Mese" },
  { value: "quarter", label: "Trimestre" },
  { value: "year", label: "Anno" },
  { value: "custom", label: "Date scelte" },
];

const HORIZON_NOUN: Record<Exclude<GoalHorizon, "custom">, string> = {
  week: "settimana",
  month: "mese",
  quarter: "trimestre",
  year: "anno",
};

export const KIND_OPTIONS: { value: GoalKind; label: string; hint: string }[] = [
  { value: "min", label: "minimo", hint: "un guadagno da raggiungere" },
  { value: "cap", label: "tetto", hint: "un guadagno da non superare" },
];

export const STATUS_TONE: Record<GoalStatus, StatusTone> = {
  raggiunto: "ok",
  "in linea": "info",
  "in ritardo": "warn",
  "non iniziato": "neutral",
  concluso: "neutral",
  "oltre il tetto": "danger",
};

/** "YYYY-MM-DD" → "dd/mm/yyyy". */
export function formatDay(day: string): string {
  return day.slice(0, 10).split("-").reverse().join("/");
}

export function periodLabel(start: string, end: string): string {
  return `${formatDay(start)} – ${formatDay(end)}`;
}

/** "+2%", "+2,5%"; a cap reads "tetto +3%". */
export function goalTargetLabel(kind: GoalKind, pct: string): string {
  const text = formatPct(pct).replace(/,?0+%$/, "%");
  return kind === "cap" ? `tetto ${text}` : text;
}

export function horizonLabel(g: Pick<Goal, "horizon" | "recurring" | "start_date" | "end_date">): string {
  if (g.horizon === "custom") return `dal ${formatDay(g.start_date)} al ${formatDay(g.end_date ?? g.start_date)}`;
  const noun = HORIZON_NOUN[g.horizon];
  return g.recurring ? `ogni ${noun}` : noun;
}

export function goalScopeLabel(g: { scope: Goal["scope"]; portfolio_name: string | null }): string {
  return g.scope === "total" ? "Totale" : (g.portfolio_name ?? "Portafoglio");
}

/** «mancano 10,00 €» / «superato di …» (minimum); «margine …» / «oltre di …» (cap). */
export function gapText(p: Pick<GoalProgress, "kind" | "gap_eur">): string | null {
  if (p.gap_eur == null) return null;
  const n = Number(p.gap_eur);
  const abs = formatMoney(p.gap_eur.replace(/^-/, ""), "EUR");
  if (p.kind === "cap") return n >= 0 ? `margine ${abs}` : `oltre di ${abs}`;
  return n > 0 ? `mancano ${abs}` : `superato di ${abs}`;
}
