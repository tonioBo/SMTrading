import type { GoalKind } from "@/api/types";
import { goalProgress } from "@/lib/signals";
import { cn } from "@/lib/utils";

/** Share of the target reached (0..100). A minimum turns green when reached; a cap turns
 * red when exceeded. A risk goal (max_loss: `pct` = the loss, `target` = the limit) turns
 * amber from 80% of the limit and red at the limit. `elapsed` (0..100, time gone in the
 * period) draws a thin marker, not for a risk goal (a loss has no pace to keep). */
export function GoalBar({ pct, target, kind = "min", elapsed, label }: { pct: string | null; target: string; kind?: GoalKind; elapsed?: string | null; label: string }) {
  const value = goalProgress(pct, target);
  const over = (kind === "cap" && pct != null && Number(pct) > Number(target)) || (kind === "max_loss" && value >= 100);
  const near = kind === "max_loss" && value >= 80;
  const mark = elapsed == null || kind === "max_loss" ? null : Math.min(100, Math.max(0, Number(elapsed)));
  return (
    <div
      role="progressbar"
      aria-label={label}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={value}
      className="relative h-2 w-full overflow-hidden rounded-full bg-border"
    >
      <div
        className={cn("h-full rounded-full", over ? "bg-loss" : near ? "bg-warn" : kind === "min" && value >= 100 ? "bg-gain" : "bg-accent")}
        style={{ width: `${value}%` }}
      />
      {mark != null && Number.isFinite(mark) && mark > 0 && mark < 100 && (
        <span className="absolute inset-y-0 w-0.5 bg-fg/60" style={{ left: `${mark}%` }} title="tempo trascorso" aria-hidden />
      )}
    </div>
  );
}
