import { useState } from "react";
import type { AgentPortfolioLink } from "@/api/types";
import { PortfolioModeBadge } from "@/components/PortfolioModeBadge";
import { cn } from "@/lib/utils";

const chip = "inline-flex items-center whitespace-nowrap rounded-md border px-2 py-0.5 text-xs font-medium";

/** «Trade · N» / «Lettura · N» (archived portfolios excluded); a click expands the table. */
export function AgentPortfoliosSummary({ agentName, portfolios }: { agentName: string; portfolios: AgentPortfolioLink[] }) {
  const [open, setOpen] = useState(false);
  const live = portfolios.filter((p) => !p.archived);
  if (live.length === 0) return <span className="text-muted">Nessuno</span>;
  const trade = live.filter((p) => p.permission === "trade").length;
  const read = live.length - trade;
  const toggle = () => setOpen((v) => !v);
  return (
    <div className="min-w-0">
      <div className="flex flex-wrap items-center gap-1.5 md:flex-nowrap">
        {trade > 0 && (
          <button type="button" aria-expanded={open} className={cn(chip, "border-accent/40 bg-accent/15 text-fg")} onClick={toggle}>
            Trade · {trade}
          </button>
        )}
        {read > 0 && (
          <button type="button" aria-expanded={open} className={cn(chip, "border-border bg-card text-fg")} onClick={toggle}>
            Lettura · {read}
          </button>
        )}
        <button type="button" aria-expanded={open} className="text-xs text-accent hover:underline" onClick={toggle}>
          {open ? "Nascondi" : "Dettagli"}
        </button>
      </div>
      {open && (
        <table aria-label={`Portafogli di ${agentName}`} className="mt-2 w-full text-xs">
          <thead className="text-left text-muted">
            <tr>
              <th className="py-1 pr-2 font-normal">Portafoglio</th>
              <th className="py-1 pr-2 font-normal">Permesso</th>
              <th className="py-1 font-normal">Modalità</th>
            </tr>
          </thead>
          <tbody>
            {live.map((p) => (
              <tr key={p.portfolio_id} className="border-t border-border">
                <td className="py-1 pr-2">{p.portfolio_name}</td>
                <td className="py-1 pr-2">{p.permission === "trade" ? "Trade" : "Lettura"}</td>
                <td className="py-1">{p.mode ? <PortfolioModeBadge mode={p.mode} liveState={p.live_state} /> : "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
