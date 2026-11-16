import { formatMoney, formatPct, formatSignedMoney } from "@/lib/format";
import { cn } from "@/lib/utils";

/** P&L: signed amount ("+12,50 €"), green at or above zero, red below (by the displayed
 * value); `pct` is shown beside it. Right-aligned in tables (`text-right` on the cell). */
export function Pnl({
  value,
  pct,
  currency = "EUR",
  signed = true,
  className,
}: {
  value: string | null | undefined;
  pct?: string | null;
  currency?: string;
  /** false: no "+" (a distance, a margin: coloured, not a gain) */
  signed?: boolean;
  className?: string;
}) {
  if (value == null || value === "") return <span className={cn("num text-muted", className)}>—</span>;
  const text = signed ? formatSignedMoney(value, currency) : formatMoney(value, currency);
  const tone = text.startsWith("-") ? "loss text-loss" : "gain text-gain";
  return (
    <span className={cn("num whitespace-nowrap", tone, className)}>
      {text}
      {pct != null && <span className="ml-1 text-xs">({formatPct(pct)})</span>}
    </span>
  );
}
