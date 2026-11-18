import type { GoalProgress } from "@/api/types";
import { Pnl } from "@/components/Pnl";
import { pointsText } from "@/lib/goals";

/** A total goal's share of each portfolio, under its bar: «AGG +5,02 € (+0,50 pt) · PRU
 * 0,00 €» (the server's `parts`: they add up to the total change and to its %; the points
 * are left out for a portfolio that has not moved). Nothing for a portfolio goal. */
export function GoalParts({ goal }: { goal: Pick<GoalProgress, "name" | "scope" | "parts"> }) {
  const parts = goal.parts ?? [];
  if (goal.scope !== "total" || parts.length === 0) return null;
  return (
    // a paragraph, not a list: it sits inside the goals' list items
    <p className="flex flex-wrap gap-x-1 text-xs text-muted" title="contributo di ogni portafoglio" data-testid={`goal-parts-${goal.name}`}>
      {parts.map((p, i) => {
        const moved = Number(p.change_eur) !== 0 || (p.contribution_pct != null && Number(p.contribution_pct) !== 0);
        return (
          <span key={p.portfolio_id} className="whitespace-nowrap">
            {i > 0 && <span aria-hidden="true">· </span>}
            <span>{p.name}</span> <Pnl value={p.change_eur} />
            {moved && p.contribution_pct != null && <span className="num">{` (${pointsText(p.contribution_pct)})`}</span>}
          </span>
        );
      })}
    </p>
  );
}
