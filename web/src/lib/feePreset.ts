import type { BrokerFeePreset, BrokerFeePresetEntry } from "@/api/types";
import { formatQty } from "@/lib/format";
import { assetClassLabel } from "@/lib/labels";

const isZero = (v: string) => /^-?0*([.,]0*)?$/.test(v.trim());

/** «commissione 0% · cambio valuta 0,15%» (the fixed part only when not zero). */
export function feeSummary(p: { pct: string; fixed: string; fx_pct: string }): string {
  const commission = `commissione ${formatQty(p.pct)}%${isZero(p.fixed) ? "" : ` + ${formatQty(p.fixed)} fissi`}`;
  return `${commission} · cambio valuta ${formatQty(p.fx_pct)}%`;
}

/** What a preset entry covers: its class, or (no class) stocks/ETF and the other classes. */
export function entryLabel(entry: BrokerFeePresetEntry, preset: BrokerFeePreset): string {
  if (entry.asset_class) return assetClassLabel(entry.asset_class);
  return preset.entries.some((e) => e.asset_class) ? "Azioni ed ETF (e altre classi)" : "Tutte le classi";
}

/** «<portafoglio> – <broker>[ · <classe>]», as the backend names the rules. */
export function presetRuleName(portfolioName: string, preset: BrokerFeePreset, entry: BrokerFeePresetEntry): string {
  const base = `${portfolioName.trim()} – ${preset.broker}`;
  return entry.asset_class ? `${base} · ${assetClassLabel(entry.asset_class)}` : base;
}
