import type { PortfolioMode } from "@/api/types";
import { MODE_INFO } from "@/lib/portfolioMode";
import { cn } from "@/lib/utils";

/** Where the portfolio sits on the path simulazione → paper → reale. */
export function PortfolioModeBadge({ mode, className }: { mode: PortfolioMode; className?: string }) {
  const info = MODE_INFO[mode];
  if (!info) return null;
  return (
    <span
      title={info.hint}
      aria-label={`Modalità: ${info.label.toLowerCase()}. ${info.hint}`}
      className={cn(
        "inline-flex items-center gap-1 rounded-md border px-2 py-0.5 text-xs font-semibold",
        info.className,
        className,
      )}
    >
      <span aria-hidden="true">{info.icon}</span>
      {info.label}
    </span>
  );
}
