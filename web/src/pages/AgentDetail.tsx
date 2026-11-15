import { ArrowLeft } from "lucide-react";
import { Link, useParams } from "react-router-dom";
import { useAgents, useSetAgentHalt, useSetAgentNotify } from "@/api/hooks/agents";
import type { Agent } from "@/api/types";
import { AgentWatchSection } from "@/components/AgentWatchSection";
import { PageHeader } from "@/components/PageHeader";
import { PortfolioModeBadge } from "@/components/PortfolioModeBadge";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { errorMessage } from "@/lib/errors";
import { formatDateTime } from "@/lib/format";
import { AdminFieldset } from "@/components/AdminOnly";

/** The `notify` permission (PATCH can_notify): the agent may write to the Telegram group. */
function NotifyPermission({ agent }: { agent: Agent }) {
  const setNotify = useSetAgentNotify();
  const id = `notify-${agent.id}`;
  return (
    <Card>
      <CardHeader>
        <CardTitle>Notifiche Telegram</CardTitle>
      </CardHeader>
      <CardContent className="space-y-2 text-sm">
        <div className="flex items-center gap-3">
          <Switch
            id={id}
            checked={agent.can_notify}
            disabled={setNotify.isPending}
            onCheckedChange={(can_notify) => setNotify.mutate({ id: agent.id, can_notify })}
          />
          <Label htmlFor={id}>Può inviare su Telegram (notify)</Label>
        </div>
        <p className="text-xs text-muted">I messaggi dell&apos;agente passano dalla coda della pagina Notifiche (al massimo 20 al minuto).</p>
        {setNotify.error && (
          <p role="alert" className="text-loss">
            {errorMessage(setNotify.error)}
          </p>
        )}
      </CardContent>
    </Card>
  );
}

/** The `halt` permission (PATCH can_halt): the agent may block new openings (CEO only). */
function HaltPermission({ agent }: { agent: Agent }) {
  const setHalt = useSetAgentHalt();
  const id = `halt-${agent.id}`;
  return (
    <Card>
      <CardHeader>
        <CardTitle>Blocco aperture</CardTitle>
      </CardHeader>
      <CardContent className="space-y-2 text-sm">
        <div className="flex items-center gap-3">
          <Switch
            id={id}
            checked={agent.can_halt}
            disabled={setHalt.isPending}
            onCheckedChange={(can_halt) => setHalt.mutate({ id: agent.id, can_halt })}
          />
          <Label htmlFor={id}>Può sospendere le aperture (halt)</Label>
        </div>
        <p className="text-xs text-muted">Con set_trading_halt l&apos;agente blocca le nuove aperture su tutti i portafogli o su uno: da concedere solo al CEO.</p>
        {setHalt.error && (
          <p role="alert" className="text-loss">
            {errorMessage(setHalt.error)}
          </p>
        )}
      </CardContent>
    </Card>
  );
}

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
                    <td className="py-1.5">{p.mode ? <PortfolioModeBadge mode={p.mode} liveState={p.live_state} label={p.mode_label} /> : "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </CardContent>
      </Card>
      <AdminFieldset>
        <div className="space-y-4">
          <NotifyPermission agent={agent} />
          <HaltPermission agent={agent} />
        </div>
      </AdminFieldset>
      <AgentWatchSection agentId={agent.id} />
    </div>
  );
}
