import { normalizeAmount, subtractDecimal } from "@/lib/decimal";
import type { FeeTier } from "@/api/types";

/** A tier row as typed in the editor; the last row's bound is always "oltre" (not stored). */
export interface TierRow {
  up_to: string;
  pct: string;
}

export interface TiersResult {
  tiers: FeeTier[] | null;
  /** one message per row index; `general` for list-level problems */
  rowErrors: Record<number, string>;
}

function isPositive(diff: string | null): boolean {
  return diff !== null && !diff.startsWith("-") && diff !== "0";
}

/**
 * Validates tier rows client-side: every row has a valid pct; every row but the last has a
 * positive `up_to`, strictly ascending; the last row is forced to `up_to: null` ("oltre").
 * No rows means a rule without tiers.
 */
export function buildTiers(rows: TierRow[]): TiersResult {
  const rowErrors: Record<number, string> = {};
  const tiers: FeeTier[] = [];
  let previous: string | null = null;
  rows.forEach((row, i) => {
    const last = i === rows.length - 1;
    const pct = normalizeAmount(row.pct);
    if (pct === null) rowErrors[i] = "Percentuale non valida";
    let up_to: string | null = null;
    if (!last) {
      up_to = normalizeAmount(row.up_to);
      if (up_to === null || !isPositive(subtractDecimal(up_to, "0"))) {
        rowErrors[i] ??= "Il limite deve essere un numero positivo";
        up_to = null;
      } else if (previous !== null && !isPositive(subtractDecimal(up_to, previous))) {
        rowErrors[i] ??= "I limiti devono essere crescenti";
      } else {
        previous = up_to;
      }
    }
    tiers.push({ up_to, pct: pct ?? "" });
  });
  return { tiers: rows.length ? tiers : null, rowErrors };
}
