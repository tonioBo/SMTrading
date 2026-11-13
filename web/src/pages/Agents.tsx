import { Copy, KeyRound, Plus, Power, PowerOff, Users } from "lucide-react";
import { useState, type FormEvent } from "react";
import { toast } from "sonner";
import { useAgents, useCreateAgent, useRotateAgentToken, useSetAgentActive, useSetAgentPortfolios } from "@/api/hooks/agents";
import { usePortfolios } from "@/api/hooks/portfolios";
import type { Agent, AgentPermission } from "@/api/types";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { DataTable, type Column } from "@/components/DataTable";
import { FormField, selectClass } from "@/components/FormField";
import { PageHeader } from "@/components/PageHeader";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { errorMessage } from "@/lib/errors";
import { formatDateTime } from "@/lib/format";

/** Shows a token. It lives only in the props of this dialog: closing unmounts it. */
function TokenDialog({ token, onClose }: { token: string; onClose: () => void }) {
  async function copy() {
    try {
      await navigator.clipboard.writeText(token);
      toast.success("Token copiato");
    } catch {
      toast.error("Copia non riuscita: selezionalo e copialo a mano");
    }
  }
  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Token dell'agente</DialogTitle>
          <DialogDescription>Copialo ora e custodiscilo: serve all'agente per collegarsi.</DialogDescription>
        </DialogHeader>
        <code className="block break-all rounded-md border border-border bg-card p-3 font-mono text-sm">{token}</code>
        <p role="alert" className="text-sm text-warn">
          Non sarà più visibile dopo la chiusura di questa finestra.
        </p>
        <DialogFooter>
          <Button variant="outline" onClick={() => void copy()}>
            <Copy />
            Copia
          </Button>
          <Button onClick={onClose}>Fatto</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function CreateAgentDialog({ onClose, onCreated }: { onClose: () => void; onCreated: (token: string) => void }) {
  const [name, setName] = useState("");
  const create = useCreateAgent();
  function submit(e: FormEvent) {
    e.preventDefault();
    if (!name.trim()) return;
    create.mutate(name.trim(), {
      onSuccess: (res) => {
        onCreated(res.token);
        create.reset();
      },
    });
  }
  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Nuovo agente</DialogTitle>
          <DialogDescription>Il token verrà mostrato una sola volta.</DialogDescription>
        </DialogHeader>
        <form onSubmit={submit} className="space-y-3" noValidate>
          <FormField label="Nome" error={create.error ? errorMessage(create.error) : null}>
            <Input value={name} onChange={(e) => setName(e.target.value)} autoFocus />
          </FormField>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={onClose} disabled={create.isPending}>
              Annulla
            </Button>
            <Button type="submit" disabled={create.isPending || !name.trim()}>
              Crea
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

type Permission = AgentPermission | "none";

function AssignmentsDialog({ agent, onClose }: { agent: Agent; onClose: () => void }) {
  const { data: portfolios } = usePortfolios();
  const save = useSetAgentPortfolios(agent.id);
  const [perms, setPerms] = useState<Record<string, Permission>>(() =>
    Object.fromEntries(agent.portfolios.map((p) => [p.portfolio_id, p.permission])),
  );

  // archived portfolios are not listed: their assignments are kept as they are
  const listed = new Set((portfolios ?? []).map((p) => p.id));
  const hidden = portfolios ? agent.portfolios.filter((p) => !listed.has(p.portfolio_id)) : [];

  function submit() {
    const assignments = (portfolios ?? [])
      .filter((p) => (perms[p.id] ?? "none") !== "none")
      .map((p) => ({ portfolio_id: p.id, permission: perms[p.id] as AgentPermission }));
    const kept = hidden.map((p) => ({ portfolio_id: p.portfolio_id, permission: p.permission }));
    save.mutate([...assignments, ...kept], { onSuccess: onClose });
  }

  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Portafogli assegnati a {agent.name}</DialogTitle>
          <DialogDescription>Nessuno: l'agente non vede il portafoglio. Trade include la lettura.</DialogDescription>
        </DialogHeader>
        <div className="space-y-2">
          {portfolios?.length === 0 && <p className="text-sm text-muted">Nessun portafoglio</p>}
          {portfolios?.map((p) => (
            <div key={p.id} className="flex items-center justify-between gap-3">
              <span>{p.name}</span>
              <select
                aria-label={`Permesso ${p.name}`}
                className={`${selectClass} w-32`}
                value={perms[p.id] ?? "none"}
                onChange={(e) => setPerms((s) => ({ ...s, [p.id]: e.target.value as Permission }))}
              >
                <option value="none">nessuno</option>
                <option value="read">read</option>
                <option value="trade">trade</option>
              </select>
            </div>
          ))}
          {hidden.length > 0 && (
            <p className="text-sm text-muted">
              {hidden.length === 1 ? "1 assegnazione" : `${hidden.length} assegnazioni`} a portafogli archiviati (
              {hidden.map((p) => p.portfolio_name).join(", ")}) restano invariate.
            </p>
          )}
        </div>
        {save.error && (
          <p role="alert" className="text-sm text-loss">
            {errorMessage(save.error)}
          </p>
        )}
        <DialogFooter>
          <Button variant="outline" onClick={onClose} disabled={save.isPending}>
            Annulla
          </Button>
          <Button onClick={submit} disabled={save.isPending || !portfolios}>
            Salva
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export default function Agents() {
  const { data, isLoading, isError } = useAgents();
  const setActive = useSetAgentActive();
  const rotate = useRotateAgentToken();
  const [creating, setCreating] = useState(false);
  const [token, setToken] = useState<string | null>(null);
  const [rotating, setRotating] = useState<Agent | null>(null);
  const [assigning, setAssigning] = useState<Agent | null>(null);

  function closeToken() {
    setToken(null);
    rotate.reset();
  }

  const columns: Column<Agent>[] = [
    { key: "name", header: "Nome", sortValue: (a) => a.name, cell: (a) => a.name },
    {
      key: "active",
      header: "Stato",
      sortValue: (a) => Number(a.active),
      cell: (a) => <Badge variant={a.active ? "default" : "secondary"}>{a.active ? "Attivo" : "Disattivato"}</Badge>,
    },
    {
      key: "last_seen",
      header: "Ultimo accesso",
      sortValue: (a) => a.last_seen_at ?? "",
      cell: (a) => <span className="num">{a.last_seen_at ? formatDateTime(a.last_seen_at) : "Mai"}</span>,
    },
    {
      key: "portfolios",
      header: "Portafogli",
      cell: (a) =>
        a.portfolios.length ? (
          <ul>
            {a.portfolios.map((p) => (
              <li key={p.portfolio_id}>
                {p.portfolio_name} ({p.permission})
              </li>
            ))}
          </ul>
        ) : (
          <span className="text-muted">Nessuno</span>
        ),
    },
    {
      key: "actions",
      header: "Azioni",
      align: "right",
      cell: (a) => (
        <div className="flex justify-end gap-1">
          <Button size="sm" variant="outline" aria-label={`Portafogli ${a.name}`} onClick={() => setAssigning(a)}>
            <Users />
            Portafogli
          </Button>
          <Button size="sm" variant="outline" aria-label={`Rigenera token ${a.name}`} onClick={() => setRotating(a)}>
            <KeyRound />
            Rigenera token
          </Button>
          <Button
            size="sm"
            variant="outline"
            aria-label={`${a.active ? "Disattiva" : "Attiva"} ${a.name}`}
            disabled={setActive.isPending}
            onClick={() =>
              setActive.mutate(
                { id: a.id, active: !a.active },
                { onError: (err) => toast.error(errorMessage(err)) },
              )
            }
          >
            {a.active ? <PowerOff /> : <Power />}
            {a.active ? "Disattiva" : "Attiva"}
          </Button>
        </div>
      ),
    },
  ];

  return (
    <div>
      <PageHeader title="Agenti">
        <Button onClick={() => setCreating(true)}>
          <Plus />
          Nuovo agente
        </Button>
      </PageHeader>
      {isError && <p className="mb-4 text-loss">Impossibile caricare gli agenti</p>}
      <Card>
        <CardContent className="pt-6">
          <DataTable aria-label="Agenti" columns={columns} rows={data} rowKey={(a) => a.id} loading={isLoading} empty="Nessun agente" />
        </CardContent>
      </Card>
      {creating && (
        <CreateAgentDialog
          onClose={() => setCreating(false)}
          onCreated={(t) => {
            setCreating(false);
            setToken(t);
          }}
        />
      )}
      {rotating && (
        <ConfirmDialog
          open
          onOpenChange={(open) => !open && setRotating(null)}
          title={`Rigenerare il token di ${rotating.name}?`}
          description="Il token attuale smette di funzionare subito."
          confirmLabel="Rigenera"
          destructive
          pending={rotate.isPending}
          error={rotate.error ? errorMessage(rotate.error) : null}
          onConfirm={() =>
            rotate.mutate(rotating.id, {
              onSuccess: (res) => {
                setRotating(null);
                setToken(res.token);
              },
            })
          }
        />
      )}
      {assigning && <AssignmentsDialog agent={assigning} onClose={() => setAssigning(null)} />}
      {token && <TokenDialog token={token} onClose={closeToken} />}
    </div>
  );
}
