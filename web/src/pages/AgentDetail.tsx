import { ArrowLeft } from "lucide-react";
import { Link, useParams } from "react-router-dom";
import { useAgents } from "@/api/hooks/agents";
import { AgentWatchSection } from "@/components/AgentWatchSection";
import { PageHeader } from "@/components/PageHeader";
import { PortfolioModeBadge } from "@/components/PortfolioModeBadge";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { formatDateTime } from "@/lib/format";

/** One agent: state, every portfolio assignment (archived included) and its watch data. */
export default function AgentDetail() {
  const { id = "" } = useParams();
  const { data, isLoading, isError } = useAgents();
  const agent = data?.find((a) => a.id === id);
  if (isLoading) return <p className="text-sm text-muted">Caricamento…</p>;
  if (isError) return <p className="text-loss">Impossibile caricare l'agente</p>;
  if (!agent) return <p className="text-sm text-muted">Agente non trovato</p>;
  return (
    <div className="space-y-4">
      <Link to="/agenti" className="inline-flex items-center gap-1 text-sm text-muted hover:text-fg">
        <ArrowLeft className="h-4 w-4" aria-hidden />
        Agenti
      </Link>
      <PageHeader title={agent.name}>
        <Badge variant={agent.active ? "default" : "secondary"}>{agent.active ? "Attivo" : "Disattivato"}</Badge>
      </PageHeader>
      <p className="text-sm text-muted">Ultimo accesso: {agent.last_seen_at ? formatDateTime(agent.last_seen_at) : "mai"}</p>
      <Card>
        <CardHeader>
          <CardTitle>Portafogli</CardTitle>
        </CardHeader>
        <CardContent className="overflow-x-auto">
          {agent.portfolios.length === 0 ? (
            <p className="text-sm text-muted">Nessun portafoglio assegnato</p>
          ) : (
            <table aria-label={`Portafogli di ${agent.name}`} className="w-full text-sm">
              <thead className="text-left text-muted">
                <tr>
                  <th className="py-1 pr-3 font-normal">Portafoglio</th>
                  <th className="py-1 pr-3 font-normal">Permesso</th>
                  <th className="py-1 font-normal">Modalità</th>
                </tr>
              </thead>
              <tbody>
                {agent.portfolios.map((p) => (
                  <tr key={p.portfolio_id} className="border-t border-border">
                    <td className="py-1.5 pr-3">
                      <Link to={`/portafogli/${p.portfolio_id}`} className="hover:underline">
                        {p.portfolio_name}
                      </Link>
                      {p.archived && <span className="ml-2 text-xs text-muted">(archiviato)</span>}
                    </td>
                    <td className="py-1.5 pr-3">{p.permission === "trade" ? "Trade" : "Lettura"}</td>
                    <td className="py-1.5">{p.mode ? <PortfolioModeBadge mode={p.mode} /> : "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </CardContent>
      </Card>
      <AgentWatchSection agentId={agent.id} />
    </div>
  );
}
