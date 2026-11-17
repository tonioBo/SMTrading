/** Labels of the «Obiettivi» (goals page, dashboard card, «Andamento» lines). */
import type { Goal, GoalBenchmark, GoalHorizon, GoalKind, GoalProgress, GoalStatus } from "@/api/types";
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
  { value: "max_loss", label: "perdita massima", hint: "una perdita da non superare nel periodo" },
  { value: "beat_index", label: "meglio dell'indice", hint: "fare meglio di un indice nello stesso periodo (in euro)" },
];

export const BENCHMARK_OPTIONS: { value: GoalBenchmark; label: string }[] = [
  { value: "SPY", label: "SPY (S&P 500)" },
  { value: "QQQ", label: "QQQ (Nasdaq 100)" },
  { value: "VWCE.DE", label: "VWCE (ETF mondiale)" },
];

/** The short name of an index: "VWCE.DE" → "VWCE". */
export function benchmarkLabel(symbol: string): string {
  return symbol.split(".")[0];
}

export const STATUS_TONE: Record<GoalStatus, StatusTone> = {
  raggiunto: "ok",
  "in linea": "info",
  "in ritardo": "warn",
  "non iniziato": "neutral",
  concluso: "neutral",
  "oltre il tetto": "danger",
  "nel limite": "ok",
  "vicino al limite": "warn",
  superato: "danger",
  avanti: "ok",
  indietro: "warn",
};

/** "YYYY-MM-DD" → "dd/mm/yyyy". */
export function formatDay(day: string): string {
  return day.slice(0, 10).split("-").reverse().join("/");
}

export function periodLabel(start: string, end: string): string {
  return `${formatDay(start)} – ${formatDay(end)}`;
}

function trimPct(pct: string): string {
  return formatPct(pct).replace(/,?0+%$/, "%");
}

/** "+2%", "+2,5%"; a cap reads "tetto +3%", a risk goal "perdita max 5%", an index goal
 * "meglio di VWCE" (margin 0) or "meglio di SPY +1,5 pt". */
export function goalTargetLabel(kind: GoalKind, pct: string, benchmark?: string | null): string {
  if (kind === "max_loss") return `perdita max ${trimPct(pct).replace(/^\+/, "")}`;
  if (kind === "beat_index") {
    const name = benchmark ? `di ${benchmarkLabel(benchmark)}` : "dell'indice";
    return Number(pct) === 0 ? `meglio ${name}` : `meglio ${name} ${trimPct(pct).replace("%", " pt")}`;
  }
  const text = trimPct(pct);
  return kind === "cap" ? `tetto ${text}` : text;
}

/** Percentage points: "1.25" → "+1,25 pt". */
export function pointsText(pp: string): string {
  return formatPct(pp).replace("%", " pt");
}

/** max_loss: «perdita 1,20% su un limite di 5%». */
export function lossText(p: Pick<GoalProgress, "kind" | "loss_pct" | "target_pct">): string | null {
  if (p.kind !== "max_loss" || p.loss_pct == null) return null;
  return `perdita ${formatPct(p.loss_pct).replace(/^\+/, "")} su un limite di ${trimPct(p.target_pct).replace(/^\+/, "")}`;
}

/** beat_index: «VWCE +2,00% · differenza +1,00 pt». */
export function comparisonText(p: Pick<GoalProgress, "kind" | "benchmark" | "benchmark_pct" | "diff_pp">): string | null {
  if (p.kind !== "beat_index") return null;
  const name = p.benchmark ? benchmarkLabel(p.benchmark) : "Indice";
  if (p.benchmark_pct == null) return `${name}: dati non ancora disponibili`;
  const diff = p.diff_pp == null ? "" : ` · differenza ${pointsText(p.diff_pp)}`;
  return `${name} ${formatPct(p.benchmark_pct)}${diff}`;
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
  if (p.gap_eur == null || p.kind === "max_loss" || p.kind === "beat_index") return null;
  const n = Number(p.gap_eur);
  const abs = formatMoney(p.gap_eur.replace(/^-/, ""), "EUR");
  if (p.kind === "cap") return n >= 0 ? `margine ${abs}` : `oltre di ${abs}`;
  return n > 0 ? `mancano ${abs}` : `superato di ${abs}`;
}
