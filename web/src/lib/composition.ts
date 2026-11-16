import type { CompositionGroup, CompositionPosition } from "@/api/types";
import { addDecimal } from "@/lib/decimal";

/** Named position slices of the donut; the rest become «Altri». */
export const MAX_SLICES = 8;
const CASH_COLOR = "rgb(var(--muted))";
const OTHERS_COLOR = "#94a3b8"; // slate-400
// categorical palette, readable on the light and the dark background
const PALETTE = ["#0ea5e9", "#8b5cf6", "#f59e0b", "#10b981", "#f43f5e", "#06b6d4", "#d946ef", "#84cc16"];

export interface Slice {
  key: string;
  label: string;
  value_eur: string;
  weight_pct: string | null;
  color: string;
}

function sumOrNull(values: (string | null)[]): string | null {
  let total: string | null = "0";
  for (const v of values) total = v == null || total == null ? null : addDecimal(total, v);
  return total;
}

/** The donut slices: up to MAX_SLICES priced positions (biggest first), «Altri», «Contante». */
export function donutSlices(positions: CompositionPosition[], cash: CompositionGroup | undefined): Slice[] {
  const priced = positions.filter((p) => p.value_eur != null);
  const named = priced.slice(0, MAX_SLICES);
  const rest = priced.slice(MAX_SLICES);
  const slices: Slice[] = named.map((p, i) => ({
    key: p.symbol,
    label: p.symbol,
    value_eur: p.value_eur as string,
    weight_pct: p.weight_pct,
    color: PALETTE[i % PALETTE.length],
  }));
  if (rest.length > 0) {
    slices.push({
      key: "__others",
      label: `Altri (${rest.length})`,
      value_eur: sumOrNull(rest.map((p) => p.value_eur)) ?? "0",
      weight_pct: sumOrNull(rest.map((p) => p.weight_pct)),
      color: OTHERS_COLOR,
    });
  }
  if (cash) slices.push({ ...cash, key: "cash", label: "Contante", color: CASH_COLOR });
  return slices;
}
