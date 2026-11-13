import { useState } from "react";
import { useAgents, useMcpAudit } from "@/api/hooks/agents";
import type { McpAuditEntry, McpAuditFilters } from "@/api/types";
import { DataTable, type Column } from "@/components/DataTable";
import { FormField, selectClass } from "@/components/FormField";
import { PageHeader } from "@/components/PageHeader";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { formatDateTime } from "@/lib/format";

function ParamsCell({ params }: { params: unknown }) {
  const [open, setOpen] = useState(false);
  return (
    <div>
      <Button size="sm" variant="ghost" aria-expanded={open} onClick={() => setOpen((o) => !o)}>
        Parametri
      </Button>
      {open && <pre className="mt-1 max-w-md overflow-auto rounded-md bg-card p-2 font-mono text-xs">{JSON.stringify(params, null, 2)}</pre>}
    </div>
  );
}

const EMPTY: McpAuditFilters = { agent_id: "", method: "", outcome: "", since: "" };

export default function McpLog() {
  const [filters, setFilters] = useState(EMPTY);
  const agents = useAgents();
  const { data, isLoading, isError } = useMcpAudit(filters);
  const set = (field: keyof McpAuditFilters) => (e: { target: { value: string } }) =>
    setFilters((f) => ({ ...f, [field]: e.target.value }));
  const agentName = (id: string | null) => agents.data?.find((a) => a.id === id)?.name ?? (id ? id.slice(0, 8) : "—");

  const columns: Column<McpAuditEntry>[] = [
    { key: "ts", header: "Data", cell: (e) => <span className="num">{formatDateTime(e.created_at)}</span> },
    { key: "agent", header: "Agente", cell: (e) => agentName(e.agent_id) },
    { key: "method", header: "Metodo", cell: (e) => <span className="font-mono">{e.method}</span> },
    {
      key: "outcome",
      header: "Esito",
      cell: (e) => <Badge variant={e.outcome === "ok" ? "default" : "destructive"}>{e.outcome}</Badge>,
    },
    { key: "latency", header: "Latenza", align: "right", cell: (e) => <span className="num">{e.latency_ms} ms</span> },
    { key: "params", header: "Parametri", cell: (e) => <ParamsCell params={e.params} /> },
  ];

  return (
    <div>
      <PageHeader title="Log MCP" />
      <div className="mb-4 grid gap-3 sm:grid-cols-4">
        <FormField label="Agente">
          <select className={selectClass} value={filters.agent_id} onChange={set("agent_id")}>
            <option value="">Tutti</option>
            {agents.data?.map((a) => (
              <option key={a.id} value={a.id}>
                {a.name}
              </option>
            ))}
          </select>
        </FormField>
        <FormField label="Metodo">
          <Input value={filters.method} onChange={set("method")} placeholder="place_order" />
        </FormField>
        <FormField label="Esito">
          <Input value={filters.outcome} onChange={set("outcome")} placeholder="ok" />
        </FormField>
        <FormField label="Dal giorno">
          <Input type="date" value={filters.since} onChange={set("since")} />
        </FormField>
      </div>
      {isError && <p className="mb-4 text-loss">Impossibile caricare il log</p>}
      <Card>
        <CardContent className="pt-6">
          <DataTable aria-label="Log MCP" columns={columns} rows={data} rowKey={(e) => String(e.id)} loading={isLoading} empty="Nessuna chiamata" />
        </CardContent>
      </Card>
    </div>
  );
}
