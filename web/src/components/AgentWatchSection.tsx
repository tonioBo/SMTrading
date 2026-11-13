import { Trash2, X } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";
import { useAgentAlerts, useAgentWatchlists, useCancelAlert, useDeleteWatchlist, useRemoveWatchSymbol } from "@/api/hooks/agents";
import type { AgentAlert, AgentWatchlist } from "@/api/types";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { errorMessage } from "@/lib/errors";
import { formatQty } from "@/lib/format";

const CONDITIONS: Record<string, string> = {
  price_above: "prezzo sopra",
  price_below: "prezzo sotto",
  change_pct_above: "variazione % sopra",
  change_pct_below: "variazione % sotto",
};

type Pending =
  | { kind: "symbol"; watchlist: AgentWatchlist; symbol: string }
  | { kind: "watchlist"; watchlist: AgentWatchlist }
  | { kind: "alert"; alert: AgentAlert };

/** «Watchlist e alert» of an agent: remove a symbol, delete a watchlist, cancel an alert. */
export function AgentWatchSection({ agentId }: { agentId: string }) {
  const watchlists = useAgentWatchlists(agentId);
  const alerts = useAgentAlerts(agentId);
  const removeSymbol = useRemoveWatchSymbol(agentId);
  const deleteWatchlist = useDeleteWatchlist(agentId);
  const cancelAlert = useCancelAlert(agentId);
  const [pending, setPending] = useState<Pending | null>(null);
  const active = (alerts.data ?? []).filter((a) => a.status === "active");

  const mutation = pending?.kind === "symbol" ? removeSymbol : pending?.kind === "watchlist" ? deleteWatchlist : cancelAlert;
  function close() {
    setPending(null);
    removeSymbol.reset();
    deleteWatchlist.reset();
    cancelAlert.reset();
  }
  function confirm() {
    if (!pending) return;
    const done = (msg: string) => () => {
      toast.success(msg);
      close();
    };
    if (pending.kind === "symbol") {
      removeSymbol.mutate(
        { watchlistId: pending.watchlist.id, symbol: pending.symbol },
        { onSuccess: done(`${pending.symbol} rimosso da ${pending.watchlist.name}`) },
      );
    } else if (pending.kind === "watchlist") {
      deleteWatchlist.mutate(pending.watchlist.id, { onSuccess: done(`Watchlist ${pending.watchlist.name} eliminata`) });
    } else {
      cancelAlert.mutate(pending.alert.id, { onSuccess: done(`Alert su ${pending.alert.symbol} annullato`) });
    }
  }
  const title =
    pending?.kind === "symbol"
      ? `Rimuovere ${pending.symbol} da ${pending.watchlist.name}?`
      : pending?.kind === "watchlist"
        ? `Eliminare la watchlist ${pending.watchlist.name}?`
        : pending
          ? `Annullare l'alert su ${pending.alert.symbol}?`
          : "";

  return (
    <Card aria-label="Watchlist e alert">
      <CardHeader>
        <CardTitle>Watchlist e alert</CardTitle>
        <p className="text-sm text-muted">L'agente riceve una notifica di ogni modifica.</p>
      </CardHeader>
      <CardContent className="space-y-6">
        <section aria-label="Watchlist" className="space-y-3">
          <h3 className="text-sm font-medium">Watchlist</h3>
          {watchlists.isError && <p className="text-sm text-loss">Impossibile caricare le watchlist</p>}
          {watchlists.data?.length === 0 && <p className="text-sm text-muted">Nessuna watchlist</p>}
          {watchlists.data?.map((w) => (
            <div key={w.id} aria-label={`Watchlist ${w.name}`} className="rounded-md border border-border p-3">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <span className="font-medium">{w.name}</span>
                <Button size="sm" variant="outline" aria-label={`Elimina watchlist ${w.name}`} onClick={() => setPending({ kind: "watchlist", watchlist: w })}>
                  <Trash2 />
                  Elimina
                </Button>
              </div>
              {w.symbols.length === 0 ? (
                <p className="mt-2 text-xs text-muted">Nessun simbolo</p>
              ) : (
                <ul className="mt-2 flex max-h-40 flex-wrap gap-1 overflow-y-auto">
                  {w.symbols.map((s) => (
                    <li key={s} className="inline-flex items-center gap-1 rounded-md border border-border bg-card px-2 py-0.5 font-mono text-xs">
                      {s}
                      <button
                        type="button"
                        aria-label={`Rimuovi ${s} da ${w.name}`}
                        className="text-muted hover:text-fg"
                        onClick={() => setPending({ kind: "symbol", watchlist: w, symbol: s })}
                      >
                        <X className="h-3 w-3" />
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          ))}
        </section>
        <section aria-label="Alert attivi" className="space-y-2">
          <h3 className="text-sm font-medium">Alert attivi</h3>
          {alerts.isError && <p className="text-sm text-loss">Impossibile caricare gli alert</p>}
          {alerts.data && active.length === 0 && <p className="text-sm text-muted">Nessun alert attivo</p>}
          <ul className="space-y-1">
            {active.map((a) => (
              <li key={a.id} className="flex flex-wrap items-center justify-between gap-2 text-sm">
                <span>
                  <span className="font-mono">{a.symbol}</span> {CONDITIONS[a.condition] ?? a.condition} <span className="num">{formatQty(a.value)}</span>
                  {a.note && <span className="text-muted"> — {a.note}</span>}
                </span>
                <span className="flex items-center gap-2">
                  <Badge variant="secondary">attivo</Badge>
                  <Button size="sm" variant="outline" aria-label={`Annulla alert ${a.symbol}`} onClick={() => setPending({ kind: "alert", alert: a })}>
                    Annulla
                  </Button>
                </span>
              </li>
            ))}
          </ul>
        </section>
      </CardContent>
      {pending && (
        <ConfirmDialog
          open
          onOpenChange={(open) => !open && close()}
          title={title}
          description="L'agente riceverà una notifica della modifica."
          confirmLabel={pending.kind === "alert" ? "Annulla alert" : pending.kind === "watchlist" ? "Elimina" : "Rimuovi"}
          destructive
          pending={mutation.isPending}
          error={mutation.error ? errorMessage(mutation.error) : null}
          onConfirm={confirm}
        />
      )}
    </Card>
  );
}
