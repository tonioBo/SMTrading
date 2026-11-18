import { useState } from "react";
import { Link } from "react-router-dom";
import { useAgentCosts } from "@/api/hooks/paperclip";
import type { AgentCostMonth, AgentCostPeriod, AgentCostRow, AgentCosts as AgentCostsData } from "@/api/types";
import { DataTable, type Column } from "@/components/DataTable";
import { PageHeader } from "@/components/PageHeader";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { formatDateTime, formatDecimal, formatPct } from "@/lib/format";
import { formatUsd } from "@/lib/paperclip";

const PERIODS: { value: AgentCostPeriod; label: string }[] = [
  { value: "day", label: "Oggi" },
  { value: "week", label: "Settimana" },
  { value: "month", label: "Mese" },
];

/** USD as Paperclip reports it; the raw value is in the title. */
function Usd({ value }: { value: string }) {
  return (
    <span className="num" title={`${value} $`}>
      {formatUsd(value)}
    </span>
  );
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

const pctText = (v: string) => `${formatDecimal(v, 1)}%`;
const MONTHS = ["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio", "agosto", "settembre", "ottobre", "novembre", "dicembre"];

function monthName(key: string): string {
  const [y, m] = key.split("-").map(Number);
  return `${MONTHS[m - 1] ?? key} ${y}`;
}

/** The current month against the budget: spend so far, daily average, projection, shares. */
function MonthCard({ month }: { month: AgentCostMonth }) {
  const over = month.budget_usd != null && Number(month.projected_usd) > Number(month.budget_usd);
  const near = month.spent_pct != null && Number(month.spent_pct) >= 80;
  return (
    <Card role="region" aria-label="Mese in corso">
      <CardHeader>
        <CardTitle>Mese in corso · {monthName(month.month)}</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4 text-sm">
        <dl className="grid grid-cols-2 gap-4 sm:grid-cols-4">
          <div>
            <dt className="text-muted">Speso finora</dt>
            <dd className={`num text-lg font-medium ${near ? "text-loss" : ""}`}>
              <Usd value={month.spent_usd} />
              {month.spent_pct != null && <span className="ml-1 text-xs text-muted">({pctText(month.spent_pct)} del budget)</span>}
            </dd>
          </div>
          <div>
            <dt className="text-muted">Media giornaliera</dt>
            <dd className="num text-lg font-medium">
              <Usd value={month.daily_avg_usd} />
              <span className="ml-1 text-xs text-muted">
                ({month.days_elapsed} di {month.days_in_month} giorni)
              </span>
            </dd>
          </div>
          <div>
            <dt className="text-muted">Proiezione a fine mese</dt>
            <dd className={`num text-lg font-medium ${over ? "text-loss" : ""}`}>
              <Usd value={month.projected_usd} />
              {month.projected_pct != null && <span className="ml-1 text-xs text-muted">({pctText(month.projected_pct)} del budget)</span>}
            </dd>
          </div>
          <div>
            <dt className="text-muted">Budget mensile</dt>
            <dd className="num text-lg font-medium">
              {month.budget_usd == null ? "—" : <Usd value={month.budget_usd} />}
              <span className="ml-1 text-xs text-muted">
                {month.budget_source === "paperclip" ? "(da Paperclip)" : month.budget_source === "settings" ? "(da Impostazioni)" : "(non impostato)"}
              </span>
            </dd>
          </div>
        </dl>
        {over && (
          <p role="alert" className="text-loss">
            A questo ritmo il mese chiude oltre il budget.
          </p>
        )}
        {month.shares.length > 0 && (
          <ul aria-label="Quota per agente" className="space-y-1">
            {month.shares.map((s) => (
              <li key={`${s.agent_id ?? ""}-${s.name}`} className="flex items-center gap-2">
                <span className="w-40 truncate">{s.name}</span>
                <span className="h-2 flex-1 rounded bg-border/40">
                  <span className="block h-2 rounded bg-accent" style={{ width: `${Math.min(100, Number(s.pct ?? 0))}%` }} />
                </span>
                <span className="num w-28 text-right">
                  <Usd value={s.usd} /> · {s.pct == null ? "—" : pctText(s.pct)}
                </span>
              </li>
            ))}
          </ul>
        )}
        <p className="text-xs text-muted">Conta la spesa fatturata (a consumo API e altro); le esecuzioni incluse nell&apos;abbonamento non pesano sul budget.</p>
      </CardContent>
    </Card>
  );
}

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
      {data?.month && <MonthCard month={data.month} />}
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
