import type { AssetClass, UserRole } from "@/api/types";

/** Italian names of the asset classes (risk profiles, fees, connectors). */
export const ASSET_CLASS_LABELS: Record<AssetClass, string> = {
  us_equity: "Azioni USA",
  eu_equity: "Azioni EU",
  crypto: "Crypto",
  forex: "Forex",
};

export function assetClassLabel(value: string): string {
  return ASSET_CLASS_LABELS[value as AssetClass] ?? value;
}

/** Italian names of the order types (the order form and the real-money confirm). */
export const ORDER_TYPE_LABELS: Record<string, string> = {
  market: "A mercato",
  limit: "Limite",
  stop: "Stop",
  stop_limit: "Stop limite",
  trailing_stop: "Trailing stop",
};

/** Italian names of the order durations (engine TimeInForce). */
export const TIF_LABELS: Record<string, string> = { gtc: "Fino a revoca (gtc)", day: "Giornaliero (day)" };
/** «Valido fino a» of a manual order: no end, today's session close, or a date and time. */
export const VALID_UNTIL_LABELS = { none: "Nessun limite", close: "Chiusura di oggi", at: "Data e ora" } as const;

/** Portal user roles («Impostazioni → Utenti»). */
export const ROLE_LABELS: Record<UserRole, string> = { admin: "Amministratore", viewer: "Sola lettura" };
