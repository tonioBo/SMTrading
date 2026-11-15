import { useState } from "react";
import { Link } from "react-router-dom";
import { useAgentCosts } from "@/api/hooks/paperclip";
import type { AgentCostPeriod, AgentCostRow, AgentCosts as AgentCostsData } from "@/api/types";
import { DataTable, type Column } from "@/components/DataTable";
import { PageHeader } from "@/components/PageHeader";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { formatDateTime, formatMoney, formatPct } from "@/lib/format";

const PERIODS: { value: AgentCostPeriod; label: string }[] = [
  { value: "day", label: "Oggi" },
  { value: "week", label: "Settimana" },
  { value: "month", label: "Mese" },
];

/** USD as Paperclip reports it, it-IT format ("1.234,50 $"). */
function Usd({ value }: { value: string }) {
  return <span className="num">{formatMoney(value, "USD")}</span>;
}

const num = (v: string) => Number(v) || 0;

const columns: Column<AgentCostRow>[] = [
  {
    key: "name",
    header: "Agente",
    sortValue: (r) => r.name,
    cell: (r) => (
      <div>
        <Link to={`/agenti/${r.agent_id}`} className="font-medium hover:underline">
          {r.name}
        </Link>
        {r.paperclip_name && r.paperclip_name !== r.name && <div className="text-xs text-muted">Paperclip: {r.paperclip_name}</div>}
      </div>
    ),
  },
  { key: "api", header: "A consumo API ($)", align: "right", sortValue: (r) => num(r.api_usd), cell: (r) => <Usd value={r.api_usd} /> },
  { key: "sub", header: "Incluso nell'abbonamento ($)", align: "right", sortValue: (r) => num(r.subscription_usd), cell: (r) => <Usd value={r.subscription_usd} /> },
  { key: "other", header: "Altro ($)", align: "right", sortValue: (r) => num(r.other_usd), cell: (r) => <Usd value={r.other_usd} /> },
  { key: "runs", header: "Esecuzioni", align: "right", sortValue: (r) => r.runs, cell: (r) => <span className="num">{r.runs}</span> },
  { key: "orders", header: "Ordini", align: "right", sortValue: (r) => r.orders, cell: (r) => <span className="num">{r.orders}</span> },
  {
    key: "portfolios",
    header: "Portafogli",
    cell: (r) =>
      r.portfolios.length === 0 ? (
        <span className="text-muted">—</span>
      ) : (
        <ul className="space-y-0.5">
          {r.portfolios.map((p) => (
            <li key={p.id}>
              <Link to={`/portafogli/${p.id}`} className="hover:underline">
                {p.name}
              </Link>{" "}
              <span className="num text-muted">{p.return_pct == null ? "—" : formatPct(p.return_pct)}</span>
            </li>
          ))}
        </ul>
      ),
  },
];

type Unmatched = AgentCostsData["unmatched"][number];
const unmatchedColumns: Column<Unmatched>[] = [
  {
    key: "name",
    header: "Agente Paperclip",
    sortValue: (r) => r.name ?? r.paperclip_agent_id,
    cell: (r) => (
      <div>
        {r.name ?? "—"}
        <div className="font-mono text-xs text-muted">{r.paperclip_agent_id}</div>
      </div>
    ),
  },
  { key: "api", header: "A consumo API ($)", align: "right", sortValue: (r) => num(r.api_usd), cell: (r) => <Usd value={r.api_usd} /> },
  { key: "sub", header: "Incluso nell'abbonamento ($)", align: "right", sortValue: (r) => num(r.subscription_usd), cell: (r) => <Usd value={r.subscription_usd} /> },
  { key: "other", header: "Altro ($)", align: "right", sortValue: (r) => num(r.other_usd), cell: (r) => <Usd value={r.other_usd} /> },
];

/** «Costi agenti»: what each agent cost in Paperclip (USD) next to what it did in SMTrading. */
export default function AgentCosts() {
  const [period, setPeriod] = useState<AgentCostPeriod>("day");
  const { data, isLoading, isError } = useAgentCosts(period);
  return (
    <div className="space-y-6">
      <PageHeader title="Costi agenti">
        <div className="flex gap-1" role="group" aria-label="Periodo">
          {PERIODS.map((p) => (
            <Button key={p.value} size="sm" variant={p.value === period ? "secondary" : "ghost"} aria-pressed={p.value === period} onClick={() => setPeriod(p.value)}>
              {p.label}
            </Button>
          ))}
        </div>
      </PageHeader>
      <p className="text-sm text-muted">
        Importi in dollari come li riporta Paperclip: «a consumo API» è la spesa fatturata a token, «incluso nell&apos;abbonamento» è il valore delle esecuzioni coperte dal piano.
        {data && (
          <>
            {" "}
            {data.last_read_at ? `Ultima lettura: ${formatDateTime(data.last_read_at)}.` : "Paperclip non è ancora stato letto."}
          </>
        )}
      </p>
      {isError && <p className="text-loss">Impossibile caricare i costi degli agenti</p>}
      <Card>
        <CardContent className="pt-6">
          <DataTable aria-label="Costi per agente" columns={columns} rows={data?.agents} rowKey={(r) => r.agent_id} loading={isLoading} empty="Nessun agente" />
        </CardContent>
      </Card>
      <Card role="region" aria-label="Agenti Paperclip non abbinati">
        <CardHeader>
          <CardTitle>Agenti Paperclip non abbinati</CardTitle>
        </CardHeader>
        <CardContent className="space-y-2">
          <p className="text-sm text-muted">Agenti di Paperclip senza un agente SMTrading con lo stesso nome o abbinato in Impostazioni → Paperclip.</p>
          <DataTable
            aria-label="Costi non abbinati"
            columns={unmatchedColumns}
            rows={data?.unmatched}
            rowKey={(r) => r.paperclip_agent_id}
            loading={isLoading}
            empty="Tutti gli agenti Paperclip sono abbinati"
          />
        </CardContent>
      </Card>
    </div>
  );
}
