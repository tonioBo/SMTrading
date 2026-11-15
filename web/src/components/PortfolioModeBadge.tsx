import type { LiveState, PortfolioMode } from "@/api/types";
import { LIVE_BADGE } from "@/lib/live";
import { MODE_INFO } from "@/lib/portfolioMode";
import { cn } from "@/lib/utils";

/**
 * Where the portfolio sits on the path simulazione → paper → reale, named with its broker
 * («SMTrading (simulato)», «Alpaca · prova», «🔴 Trading 212 · reale · sospeso»): `label`
 * is the backend's `mode_label`, shown as it is. A portfolio linked to a real-money link
 * is always red, whatever its state.
 */
export function PortfolioModeBadge({
  mode,
  liveState,
  label,
  className,
}: {
  mode: PortfolioMode;
  liveState?: LiveState | null;
  label?: string | null;
  className?: string;
}) {
  const live = liveState ? LIVE_BADGE[liveState] : undefined;
  const base = MODE_INFO[live ? "reale" : mode];
  if (!base) return null;
  const text = label || base.label;
  const hint = live?.hint ?? base.hint;
  return (
    <span
      title={hint}
      aria-label={`Modalità: ${text}. ${hint}`}
      data-live-state={liveState ?? undefined}
      className={cn(
        "inline-flex items-center gap-1 rounded-md border px-2 py-0.5 text-xs font-semibold",
        base.className,
        className,
      )}
    >
      {text}
    </span>
  );
}
