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

/** Portal user roles («Impostazioni → Utenti»). */
export const ROLE_LABELS: Record<UserRole, string> = { admin: "Amministratore", viewer: "Sola lettura" };
