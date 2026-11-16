import type { BrokerFeePreset } from "@/api/types";
import { formatQty } from "@/lib/format";

const isZero = (v: string) => /^-?0*([.,]0*)?$/.test(v.trim());

/** «commissione 0% · cambio valuta 0,15%» (the fixed part only when not zero). */
export function presetSummary(p: Pick<BrokerFeePreset, "pct" | "fixed" | "fx_pct">): string {
  const commission = `commissione ${formatQty(p.pct)}%${isZero(p.fixed) ? "" : ` + ${formatQty(p.fixed)} fissi`}`;
  return `${commission} · cambio valuta ${formatQty(p.fx_pct)}%`;
}
