import { formatMoney, formatPct } from "@/lib/format";
import { cn } from "@/lib/utils";

/** P&L with gain/loss colouring (by the displayed sign); `pct` is shown beside the amount. */
export function Pnl({
  value,
  pct,
  currency = "EUR",
}: {
  value: string | null | undefined;
  pct?: string | null;
  currency?: string;
}) {
  if (value == null) return <span className="num text-muted">—</span>;
  const text = formatMoney(value, currency);
  const tone = text.startsWith("-") ? "loss text-loss" : /[1-9]/.test(text) ? "gain text-gain" : "text-muted";
  return (
    <span className={cn("num", tone)}>
      {text}
      {pct != null && <span className="ml-1 text-xs">({formatPct(pct)})</span>}
    </span>
  );
}
