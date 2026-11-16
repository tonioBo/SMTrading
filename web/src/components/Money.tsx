import { formatMoney } from "@/lib/format";
import { cn } from "@/lib/utils";

/** An amount in `currency` (default EUR), it-IT formatted, tabular figures; null shows an
 * em dash. In a table its cell is right-aligned (`text-right`). */
export function Money({ value, currency = "EUR", className }: { value: string | null | undefined; currency?: string; className?: string }) {
  return <span className={cn("num whitespace-nowrap", className)}>{value == null || value === "" ? "—" : formatMoney(value, currency)}</span>;
}
