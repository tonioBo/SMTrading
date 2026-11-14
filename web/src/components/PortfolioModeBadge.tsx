import type { LiveState, PortfolioMode } from "@/api/types";
import { LIVE_BADGE } from "@/lib/live";
import { MODE_INFO } from "@/lib/portfolioMode";
import { cn } from "@/lib/utils";

/**
 * Where the portfolio sits on the path simulazione → paper → reale. A portfolio linked to
 * a real-money link is always red, whatever its state: REALE, REALE · PROVA, REALE · SOSPESO.
 */
export function PortfolioModeBadge({
  mode,
  liveState,
  className,
}: {
  mode: PortfolioMode;
  liveState?: LiveState | null;
  className?: string;
}) {
  const live = liveState ? LIVE_BADGE[liveState] : undefined;
  const base = MODE_INFO[live ? "reale" : mode];
  if (!base) return null;
  const label = live?.label ?? base.label;
  const hint = live?.hint ?? base.hint;
  return (
    <span
      title={hint}
      aria-label={`Modalità: ${label.toLowerCase()}. ${hint}`}
      data-live-state={liveState ?? undefined}
      className={cn(
        "inline-flex items-center gap-1 rounded-md border px-2 py-0.5 text-xs font-semibold",
        base.className,
        className,
      )}
    >
      <span aria-hidden="true">{base.icon}</span>
      {label}
    </span>
  );
}
