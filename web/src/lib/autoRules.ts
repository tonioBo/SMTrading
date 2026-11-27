import type { AutoRuleBody, AutoValue, ModelParam, ModelSpec } from "@/api/types";

/** What the page shows for the codes of the automatic rules (the server speaks in codes). */

export const MODEL_LABELS: Record<string, string> = {
  breakout: "Rottura del massimo",
  mean_reversion: "Ritorno alla media",
  vwap_pullback: "Ritracciamento sul VWAP",
};

export const EXIT_REASON_LABELS: Record<string, string> = {
  stop: "stop",
  target: "obiettivo",
  time: "a tempo",
  session: "fine seduta",
  day_close: "rete di fine seduta",
  manual: "manuale",
  end: "fine dei dati",
};

export const TRADE_STATUS_LABELS: Record<string, string> = {
  sent: "inviata",
  open: "aperta",
  closed: "chiusa",
  canceled: "annullata",
  rejected: "rifiutata",
};

/** The entries the guards blocked, by reason. */
export const BLOCKED_LABELS: Record<string, string> = {
  stale: "prezzo vecchio",
  spread: "forbice larga",
  closed: "mercato chiuso",
  busy: "operazione già aperta",
  no_stream: "senza flusso",
  broker_down: "broker non risponde",
  braked: "fermata dai freni",
  size: "importo troppo piccolo",
  rejected: "rifiutata dai controlli di rischio",
  inactive: "regola non più attiva",
  bars_stale: "barre al minuto ferme",
  price_moved: "prezzo cambiato all'invio",
};

export const PARAM_LABELS: Record<string, { label: string; help: string }> = {
  lookback_min: { label: "Finestra (minuti)", help: "Quanti minuti di storico guarda il modello." },
  volume_mult: { label: "Volume minimo (multiplo della media)", help: "Vuoto: nessun controllo sul volume. Altrimenti l'ultimo minuto deve avere almeno questo multiplo del volume medio." },
  z: { label: "Scarto dalla media (z)", help: "Entra quando il prezzo scende di almeno questo numero di deviazioni standard sotto la media." },
  pullback_pct: { label: "Distanza massima dal VWAP (%)", help: "Entra solo se il prezzo è al massimo questa percentuale sopra il VWAP di seduta." },
  min_above_min: { label: "Minuti sopra il VWAP", help: "Quanti minuti chiusi di fila sopra il VWAP prima del ritracciamento." },
  stop_pct: { label: "Stop (%)", help: "Perdita massima sul prezzo d'entrata prima dell'uscita." },
  target_pct: { label: "Obiettivo (%)", help: "Guadagno sul prezzo d'entrata a cui si esce." },
  max_hold_s: { label: "Durata massima (secondi)", help: "Dopo questo tempo si esce comunque, a mercato." },
  no_entry_first_min: { label: "Niente entrate nei primi minuti", help: "Minuti dall'apertura della seduta in cui non si entra." },
  no_entry_last_min: { label: "Niente entrate negli ultimi minuti", help: "Minuti prima della chiusura in cui non si entra." },
  cooldown_s: { label: "Pausa dopo un'operazione (secondi)", help: "Attesa minima prima di una nuova entrata sullo stesso simbolo." },
  entry: { label: "Tipo di entrata", help: "A mercato subito, oppure ordine limite poco sotto il prezzo." },
  limit_offset_pct: { label: "Distanza del limite (%)", help: "Solo con l'entrata a limite: quanto sotto il prezzo si mette il limite. Deve restare sotto l'obiettivo." },
  entry_timeout_s: { label: "Attesa del limite (secondi)", help: "Solo con l'entrata a limite: dopo questo tempo l'ordine non riempito viene annullato." },
  max_age_s: { label: "Età massima del prezzo (secondi)", help: "Con un prezzo più vecchio non si entra." },
  max_spread_pct: { label: "Forbice massima (%)", help: "Con una forbice più larga non si entra." },
};

export const CHOICE_LABELS: Record<string, string> = { market: "a mercato", limit: "a limite" };

/** «0.5» → «0,5» */
export const it = (v: AutoValue | undefined): string => (v == null ? "" : String(v).replace(".", ","));

/** The text of a form field for a stored value. */
export const asText = (v: AutoValue | undefined): string => (v == null ? "" : String(v));

const INT = /^\d+$/;
const DEC = /^\d+(?:[.,]\d+)?$/;

/** A decimal typed with a point or a comma, as the numeric string the API wants; null if not a number. */
export function decimalText(text: string): string | null {
  const t = text.trim();
  return DEC.test(t) ? t.replace(",", ".") : null;
}

export type Parsed<T> = { value: T } | { error: string };

/** One model parameter typed in a field: the value to send, or the message for the field. */
export function parseParam(p: ModelParam, text: string): Parsed<AutoValue> {
  const label = PARAM_LABELS[p.name]?.label ?? p.name;
  const t = text.trim();
  if (p.kind === "choice") return p.choices.includes(t) ? { value: t } : { error: `${label}: scegli un valore` };
  if (t === "") return p.kind === "opt_dec" ? { value: null } : { error: `${label}: obbligatorio` };
  let value: string | number;
  if (p.kind === "int") {
    if (!INT.test(t)) return { error: `${label}: un numero intero` };
    value = Number(t);
  } else {
    const d = decimalText(t);
    if (d === null) return { error: `${label}: un numero` };
    value = d;
  }
  const n = Number(value);
  const lo = p.lo == null ? null : Number(p.lo);
  const hi = p.hi == null ? null : Number(p.hi);
  if ((lo !== null && n < lo) || (hi !== null && n > hi)) return { error: `${label}: fuori dai limiti ${it(p.lo)}–${it(p.hi)}` };
  return { value };
}

export interface BrakeSpec {
  key: string;
  label: string;
  help: string;
  kind: "int" | "dec";
  lo: number;
  hi: number | null;
}

/** The brakes of a rule; the portfolio adds the cap on open positions. */
export const RULE_BRAKES: BrakeSpec[] = [
  { key: "max_trades_hour", label: "Operazioni all'ora", help: "da 1 a 120", kind: "int", lo: 1, hi: 120 },
  { key: "max_trades_day", label: "Operazioni al giorno", help: "da 1 a 1000", kind: "int", lo: 1, hi: 1000 },
  { key: "max_loss_day", label: "Perdita massima del giorno (importo)", help: "nella valuta del portafoglio, maggiore di 0", kind: "dec", lo: 0, hi: null },
  { key: "max_loss_day_pct", label: "Perdita massima del giorno (%)", help: "oltre 0, fino a 50", kind: "dec", lo: 0, hi: 50 },
  { key: "max_consecutive_losses", label: "Perdite di fila", help: "da 1 a 20", kind: "int", lo: 1, hi: 20 },
];
export const PORTFOLIO_BRAKES: BrakeSpec[] = [
  ...RULE_BRAKES,
  { key: "max_open_positions", label: "Posizioni aperte insieme", help: "da 1 a 30", kind: "int", lo: 1, hi: 30 },
];

/** The brakes a new rule / a portfolio starts with (the server's defaults). */
export const RULE_BRAKE_DEFAULTS: Record<string, string> = { max_trades_hour: "20", max_trades_day: "100", max_loss_day: "", max_loss_day_pct: "1", max_consecutive_losses: "3" };
export const PORTFOLIO_BRAKE_DEFAULTS: Record<string, string> = { max_trades_hour: "60", max_trades_day: "300", max_loss_day: "", max_loss_day_pct: "2", max_consecutive_losses: "5", max_open_positions: "5" };

export const BRAKES_HELP = "Lasciato vuoto, il freno è spento.";

/** The form values of stored brakes (a missing key takes the default, null is «spento»). */
export function brakesToForm(stored: Record<string, AutoValue> | undefined, defaults: Record<string, string>): Record<string, string> {
  const out: Record<string, string> = {};
  for (const k of Object.keys(defaults)) out[k] = stored && k in stored ? asText(stored[k]) : defaults[k];
  return out;
}

/** The brakes typed in the form: what to send (a blank one is null = off), or the errors by key. */
export function parseBrakes(values: Record<string, string>, specs: BrakeSpec[]): { brakes: Record<string, AutoValue>; errors: Record<string, string> } {
  const brakes: Record<string, AutoValue> = {};
  const errors: Record<string, string> = {};
  for (const s of specs) {
    const t = (values[s.key] ?? "").trim();
    if (t === "") {
      brakes[s.key] = null;
      continue;
    }
    if (s.kind === "int") {
      const n = INT.test(t) ? Number(t) : NaN;
      if (!(n >= s.lo && (s.hi === null || n <= s.hi))) errors[s.key] = `${s.label}: un numero intero (${s.help})`;
      else brakes[s.key] = n;
    } else {
      const d = decimalText(t);
      const n = d === null ? NaN : Number(d);
      if (!(n > s.lo && (s.hi === null || n <= s.hi))) errors[s.key] = `${s.label}: un numero (${s.help})`;
      else brakes[s.key] = d;
    }
  }
  return { brakes, errors };
}

// --- the rule form -------------------------------------------------------------------

export interface RuleFormState {
  portfolioId: string;
  symbol: string;
  model: string;
  params: Record<string, string>;
  size: "qty" | "notional";
  amount: string;
  brakes: Record<string, string>;
}

/** Every parameter of the model at its default (create) or at the rule's saved value (edit). */
export function paramsToForm(spec: ModelSpec | undefined, saved?: Record<string, AutoValue>): Record<string, string> {
  const out: Record<string, string> = {};
  for (const p of spec?.params ?? []) out[p.name] = asText(saved && p.name in saved ? saved[p.name] : p.default);
  return out;
}

/** The body of the POST/PATCH from the form, or the errors by field (`params.<name>`, `brakes.<key>`, `amount`). */
export function buildRuleBody(spec: ModelSpec, f: RuleFormState): { body: AutoRuleBody; errors: Record<string, string> } {
  const errors: Record<string, string> = {};
  const params: Record<string, AutoValue> = {};
  for (const p of spec.params) {
    const r = parseParam(p, f.params[p.name] ?? "");
    if ("error" in r) errors[`params.${p.name}`] = r.error;
    else params[p.name] = r.value;
  }
  const amount = decimalText(f.amount);
  if (amount === null || !(Number(amount) > 0)) errors.amount = f.size === "qty" ? "Quantità: un numero maggiore di 0" : "Importo: un numero maggiore di 0";
  const b = parseBrakes(f.brakes, RULE_BRAKES);
  for (const [k, m] of Object.entries(b.errors)) errors[`brakes.${k}`] = m;
  const body: AutoRuleBody = { params, brakes: b.brakes };
  if (amount !== null) body[f.size] = amount;
  return { body, errors };
}

