import type { BrokerKind } from "@/api/types";

export const BROKER_KINDS: { value: BrokerKind; label: string; keys: string[] }[] = [
  { value: "alpaca_paper", label: "Alpaca Paper", keys: ["key_id", "secret_key"] },
  { value: "t212_demo", label: "Trading 212 Demo", keys: ["api_key", "api_secret"] },
];

export const brokerKindLabel = (kind: string) => BROKER_KINDS.find((k) => k.value === kind)?.label ?? kind;

export const SECRET_LABELS: Record<string, string> = {
  key_id: "Key ID",
  secret_key: "Secret key",
  api_key: "API key",
  api_secret: "API secret",
};

/** Order.broker_status: local states (BrokerSync work) and the broker's own states. */
export const BROKER_STATUS_LABELS: Record<string, string> = {
  to_send: "Da inviare",
  sending: "Invio in corso",
  unknown: "Esito sconosciuto",
  cancel_requested: "Annullamento richiesto",
  replace_requested: "Modifica richiesta",
  accepted: "Accettato",
  partially_filled: "Parzialmente eseguito",
  filled: "Eseguito",
  canceled: "Annullato",
  rejected: "Rifiutato",
  expired: "Scaduto",
};

export const LINK_STATUS_LABELS: Record<string, string> = { ok: "Operativo", error: "Errore", unknown: "Non verificato" };
