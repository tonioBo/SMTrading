import { ApiError } from "@/api/client";
import type { LiveKind, LiveState } from "@/api/types";

/** The real-money kinds, shown with a red border on the Broker page. `labels`: key field labels of the kind (else SECRET_LABELS). */
export const LIVE_KINDS: { value: LiveKind; label: string; broker: string; keys: string[]; labels?: Record<string, string> }[] = [
  { value: "alpaca_live", label: "Alpaca — denaro reale", broker: "Alpaca", keys: ["key_id", "secret_key"] },
  { value: "t212_live", label: "Trading 212 — denaro reale", broker: "Trading 212", keys: ["api_key", "api_secret"] },
  {
    value: "etoro_live",
    label: "eToro — denaro reale",
    broker: "eToro",
    keys: ["api_key", "user_key"],
    labels: { api_key: "Chiave API (x-api-key)", user_key: "Chiave utente reale (x-user-key)" },
  },
];

export const isLiveKind = (kind: string): boolean => LIVE_KINDS.some((k) => k.value === kind);
export const liveKindInfo = (kind: string) => LIVE_KINDS.find((k) => k.value === kind);

/** The badge text after 🔴 per live state (spec §3). */
export const LIVE_BADGE: Record<LiveState, { label: string; hint: string }> = {
  armed: { label: "REALE", hint: "Denaro reale: gli ordini vanno al conto vero" },
  dry_run: { label: "REALE · PROVA", hint: "Prova a secco: gli ordini si fermano prima dell'invio" },
  configured: { label: "REALE · SOSPESO", hint: "Collegamento reale sospeso: nessun ordine viene inviato" },
};

export const LIVE_STATE_LABELS: Record<LiveState, string> = {
  configured: "Sospeso",
  dry_run: "Prova a secco",
  armed: "Denaro reale attivo",
};

/** live_audit.action → Italian label. */
export const LIVE_AUDIT_ACTIONS: Record<string, string> = {
  keys_saved: "Chiavi salvate",
  test_ok: "Test riuscito",
  test_failed: "Test fallito",
  dry_run_on: "Prova a secco avviata",
  dry_run_off: "Prova a secco terminata",
  armed: "Denaro reale attivato",
  disarmed: "Denaro reale disattivato",
  portfolio_linked: "Portafoglio collegato",
  portfolio_unlinked: "Portafoglio scollegato",
  reduce_only_auto: "Reduce-only automatico",
  resumed: "Ripresa dopo verifica",
  kill_switch: "Kill switch",
  flatten: "Chiusura di tutte le posizioni",
  order_sent: "Ordine inviato",
};

/** Minimum Trading 212 API key permissions (no «Pies - Write»). */
export const T212_PERMISSIONS = [
  "Dati del conto (Account data)",
  "Metadati (Metadata)",
  "Portafoglio (Portfolio)",
  "Ordini: lettura (Orders - Read)",
  "Ordini: esecuzione (Orders - Execute)",
  "Storico (History)",
];

/** A reduce-only set by a real-money anomaly: only «Ho verificato, riprendi» (TOTP) lifts it. */
export const isLiveReason = (reason: string | null | undefined): boolean => Boolean(reason?.startsWith("live_"));

export const TOTP_RE = /^\d{6}$/;
export const TOTP_REQUIRED_MESSAGE = "Attiva prima l'autenticazione a due fattori";
export const TRAILING_NOTE = "stop gestito da SMTrading, non dal broker";

export const TELEGRAM_DOWN_TEXT = "Telegram non funziona: gli ordini reali continuano, le notifiche restano in coda";

export function liveBannerText(n: number): string {
  return `Denaro reale attivo su ${n} ${n === 1 ? "portafoglio" : "portafogli"}`;
}

/** The admin has no TOTP: the real-money action cannot go on. A wrong code is FORBIDDEN on
 * the totp field too, so only the message tells the two apart. */
export function isTotpMissing(err: unknown): boolean {
  return err instanceof ApiError && err.code === "FORBIDDEN" && err.message === TOTP_REQUIRED_MESSAGE;
}

/** Causes of an automatic real-money reduce-only (`live_<cause>`, brokers.live.protect). */
const LIVE_CAUSES: Record<string, string> = {
  reconcile_diff: "posizioni diverse da quelle del broker",
  order_unknown: "esito sconosciuto di un ordine",
  fingerprint_changed: "conto cambiato",
  cash_shortfall: "liquidità al broker inferiore a quella del portafoglio",
  fuse: "fusibile: troppi ordini reali nell'ultima ora",
  health: "broker non raggiungibile",
  margin_account: "il conto non è più un conto cash (margine o leva attivi)",
};

const REDUCE_ONLY_REASONS: Record<string, string> = {
  daily_loss: "Perdita giornaliera massima raggiunta",
  broker_liquidation: "Liquidazione da parte del broker",
};

/** `reduce_only_reason` in Italian; an unknown code is shown as it is. */
export function reduceOnlyReasonLabel(reason: string): string {
  if (isLiveReason(reason)) {
    const cause = reason.slice("live_".length);
    return `Anomalia del denaro reale: ${LIVE_CAUSES[cause] ?? cause}`;
  }
  return REDUCE_ONLY_REASONS[reason] ?? reason;
}

export const DISARM_LABEL = "Disattiva denaro reale";
export const DISARM_RETRY_LABEL = "Ripeti disattivazione / sistema ordini";
export const DISARM_UNSETTLED = "Disattivato, ordini da sistemare: riprova";
