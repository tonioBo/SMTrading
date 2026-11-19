import { useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { useAgentResults } from "@/api/hooks/agentResults";
import type { AgentResultRow, AgentResultsPeriod, AgentResultsTeam, DecisionKind, StrategyResultRow } from "@/api/types";
import { DataTable, type Column } from "@/components/DataTable";
import { Money } from "@/components/Money";
import { PageHeader } from "@/components/PageHeader";
import { Pnl } from "@/components/Pnl";
import { StatusBadge, type StatusTone } from "@/components/StatusBadge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { formatDateTime, formatDecimal, formatHolding } from "@/lib/format";
import { formatDay } from "@/lib/goals";
import { formatUsd } from "@/lib/paperclip";
import { journalLink } from "@/lib/trades";

export const NO_STRATEGY = "Senza strategia";

const PERIODS: { value: AgentResultsPeriod; label: string }[] = [
  { value: "week", label: "Settimana in corso" },
  { value: "1m", label: "1 mese" },
  { value: "3m", label: "3 mesi" },
  { value: "all", label: "Tutto" },
];

const KIND_LABEL: Record<DecisionKind, string> = {
  proposal: "proposte",
  approval: "approvazioni",
  veto: "veti",
  allocation: "allocazioni",
  strategy: "strategie",
  risk_rule: "regole di rischio",
  lesson: "lezioni",
  team_rule: "regole del team",
  other: "altro",
};
const KIND_ORDER = Object.keys(KIND_LABEL) as DecisionKind[];

const STATUS_TONE: Record<string, StatusTone> = {
  approvata: "ok",
  modificata: "ok",
  sospesa: "warn",
  revocata: "neutral",
  respinta: "neutral",
};

const num = (v: string | null | undefined) => (v == null ? -Infinity : Number(v) || 0);
const pct = (v: string | null) => (v == null ? "—" : `${formatDecimal(v, 1)}%`);

function Usd({ value }: { value: string | null }) {
  if (value == null) return <span className="num text-muted">—</span>;
  return (
    <span className="num" title={`${value} $`}>
      {formatUsd(value)}
    </span>
  );
}

/** "proposte 3 · veti 1": the kinds with at least one decision. */
function kindsText(d: AgentResultRow["decisions"]): string {
  return KIND_ORDER.filter((k) => (d[k] ?? 0) > 0)
    .map((k) => `${KIND_LABEL[k]} ${d[k]}`)
    .join(" · ");
}

const agentColumns: Column<AgentResultRow>[] = [
  {
    key: "name",
    header: "Agente",
    sortValue: (r) => r.name,
    cell: (r) => (
      <div>
        <Link to={`/agenti/${r.agent_id}`} className="font-medium hover:underline">
          {r.name}
        </Link>
        {!r.active && <span className="ml-1 text-xs text-muted">(disattivato)</span>}
        {r.paperclip_names.length > 0 && r.paperclip_names.join(", ").toLowerCase() !== r.name.toLowerCase() && (
          <div className="text-xs text-muted">Paperclip: {r.paperclip_names.join(", ")}</div>
        )}
      </div>
    ),
  },
  {
    key: "decisions",
    header: "Decisioni",
    sortValue: (r) => r.decisions_total,
    cell: (r) => (
      <div>
        <span className="num">{r.decisions_total}</span>
        {r.decisions_total > 0 && <div className="text-xs text-muted">{kindsText(r.decisions)}</div>}
      </div>
    ),
  },
  {
    key: "proposals",
    header: "Proposte",
    align: "right",
    sortValue: (r) => r.proposals,
    cell: (r) =>
      r.proposals === 0 ? (
        <span className="num text-muted">0</span>
      ) : (
        <div className="num">
          {r.proposals}
          <div className="text-xs text-muted">{`${r.approved} approvate · ${r.vetoed} veti · ${r.pending} in attesa`}</div>
        </div>
      ),
  },
  { key: "rate", header: "Approvate (%)", align: "right", sortValue: (r) => num(r.approval_rate_pct), cell: (r) => <span className="num">{pct(r.approval_rate_pct)}</span> },
  { key: "written", header: "Lezioni scritte", align: "right", sortValue: (r) => r.lessons_written, cell: (r) => <span className="num">{r.lessons_written}</span> },
  {
    key: "about",
    header: "Lezioni che lo riguardano",
    align: "right",
    sortValue: (r) => r.lessons_about,
    cell: (r) => (
      <span className="num">
        {r.lessons_about}
        {r.lessons_repeated > 0 && <span className="ml-1 text-xs text-loss">{`(${r.lessons_repeated} ripetute)`}</span>}
      </span>
    ),
  },
  {
    key: "mcp",
    header: "Chiamate MCP",
    align: "right",
    sortValue: (r) => r.mcp_calls,
    cell: (r) => (
      <div className="num">
        {r.mcp_calls}
        {r.mcp_calls > 0 && (
          <div className={`text-xs ${r.mcp_errors > 0 ? "text-loss" : "text-muted"}`}>{`${r.mcp_errors} errori (${pct(r.mcp_error_pct)})`}</div>
        )}
      </div>
    ),
  },
  { key: "orders", header: "Ordini eseguiti", align: "right", sortValue: (r) => r.orders_executed, cell: (r) => <span className="num">{r.orders_executed}</span> },
  {
    key: "cost",
    header: "Costo ($)",
    align: "right",
    sortValue: (r) => num(r.cost_usd),
    cell: (r) => (
      <div>
        <Usd value={r.cost_usd} />
        {r.runs > 0 && <div className="text-xs text-muted">{`${r.runs} esecuzioni`}</div>}
      </div>
    ),
  },
  { key: "per-order", header: "Costo per ordine ($)", align: "right", sortValue: (r) => num(r.cost_per_order_usd), cell: (r) => <Usd value={r.cost_per_order_usd} /> },
];

const strategyColumns: Column<StrategyResultRow>[] = [
  {
    key: "ref",
    header: "Strategia",
    sortValue: (r) => r.ref ?? "~",
    cell: (r) => (
      <div>
        <Link to={journalLink({ strategy: r.ref ?? "none" })} className="num block font-semibold text-accent hover:underline" aria-label={`Operazioni ${r.ref ?? NO_STRATEGY}`}>
          {r.ref ?? NO_STRATEGY}
        </Link>
        {r.name && <div className="text-xs text-muted">{r.name}</div>}
        {r.portfolios.length > 0 && <div className="text-xs text-muted">{r.portfolios.join(", ")}</div>}
      </div>
    ),
  },
  {
    key: "status",
    header: "Stato",
    sortValue: (r) => r.status ?? "",
    cell: (r) => (r.status ? <StatusBadge tone={STATUS_TONE[r.status] ?? "neutral"}>{r.status}</StatusBadge> : <span className="text-muted">—</span>),
  },
  { key: "opened", header: "Aperte", align: "right", sortValue: (r) => r.trades_opened, cell: (r) => <span className="num">{r.trades_opened}</span> },
  { key: "closed", header: "Chiuse", align: "right", sortValue: (r) => r.trades_closed, cell: (r) => <span className="num">{r.trades_closed}</span> },
  {
    key: "win",
    header: "Vincenti",
    align: "right",
    sortValue: (r) => num(r.win_rate_pct),
    cell: (r) => (
      <span className="num">
        {pct(r.win_rate_pct)}
        {r.trades_closed > 0 && <span className="ml-1 text-xs text-muted">{`(${r.wins}/${r.trades_closed})`}</span>}
      </span>
    ),
  },
  { key: "real", header: "Realizzato", align: "right", sortValue: (r) => num(r.realized_pnl_eur), cell: (r) => <Pnl value={r.realized_pnl_eur} /> },
  { key: "costs", header: "Costi", align: "right", sortValue: (r) => num(r.costs_eur), cell: (r) => <Money value={r.costs_eur} /> },
  { key: "net", header: "Netto", align: "right", sortValue: (r) => num(r.net_pnl_eur), cell: (r) => <Pnl value={r.net_pnl_eur} className="font-semibold" /> },
  { key: "hold", header: "Durata media", align: "right", sortValue: (r) => r.avg_holding_s ?? -1, cell: (r) => <span className="num whitespace-nowrap">{formatHolding(r.avg_holding_s)}</span> },
  { key: "open", header: "Posizioni aperte", align: "right", sortValue: (r) => r.open_positions, cell: (r) => <span className="num">{r.open_positions}</span> },
];

function Tile({ label, children, hint }: { label: string; children: ReactNode; hint?: ReactNode }) {
  return (
    <div>
      <dt className="text-muted">{label}</dt>
      <dd className="text-lg font-medium">{children}</dd>
      {hint && <dd className="text-xs text-muted">{hint}</dd>}
    </div>
  );
}

function TeamCard({ team }: { team: AgentResultsTeam }) {
  return (
    <Card role="region" aria-label="Riepilogo del team">
      <CardHeader>
        <CardTitle>Riepilogo del team</CardTitle>
      </CardHeader>
      <CardContent>
        <dl className="grid grid-cols-2 gap-4 text-sm sm:grid-cols-3 lg:grid-cols-5">
          <Tile label="Risultato netto" hint={team.costs_eur != null ? <>costi <Money value={team.costs_eur} /> già tolti</> : undefined}>
            <Pnl value={team.pnl_eur} />
          </Tile>
          <Tile label="Ordini eseguiti">
            <span className="num">{team.orders_executed}</span>
          </Tile>
          <Tile label="Proposte" hint={`${team.approvals} approvazioni · approvate ${pct(team.approval_rate_pct)}`}>
            <span className="num">{team.proposals}</span>
          </Tile>
          <Tile label="Veti">
            <span className="num">{team.vetoes}</span>
          </Tile>
          <Tile label="Lezioni" hint={team.lessons_repeated > 0 ? <span className="text-loss">{`${team.lessons_repeated} ripetute`}</span> : "nessuna ripetuta"}>
            <span className="num">{team.lessons}</span>
          </Tile>
          <Tile label="Regole del team attive" hint="oggi">
            <span className="num">{team.rules_active}</span>
          </Tile>
          <Tile label="Costo agenti" hint={<>fatturato <Usd value={team.billed_usd} /></>}>
            <Usd value={team.cost_usd} />
          </Tile>
          <Tile label="Risultato per 1 $ di costo">
            {team.pnl_per_usd == null ? <span className="num text-muted">—</span> : <Pnl value={team.pnl_per_usd} />}
          </Tile>
          <Tile label="Costo per ordine eseguito">
            <Usd value={team.cost_per_order_usd} />
          </Tile>
          {Number(team.unmatched_cost_usd) > 0 && (
            <Tile label="Agenti Paperclip non abbinati" hint="compresi nel costo agenti">
              <Usd value={team.unmatched_cost_usd} />
            </Tile>
          )}
        </dl>
      </CardContent>
    </Card>
  );
}

/** «Risultati agenti»: what each agent did and cost, each strategy's results, the team. */
export default function AgentResults() {
  const [period, setPeriod] = useState<AgentResultsPeriod>("week");
  const { data, isLoading, isError } = useAgentResults(period);
  return (
    <div className="space-y-6">
      <PageHeader title="Risultati agenti">
        <div className="flex flex-wrap gap-1" role="group" aria-label="Periodo">
          {PERIODS.map((p) => (
            <Button key={p.value} size="sm" variant={p.value === period ? "secondary" : "ghost"} aria-pressed={p.value === period} onClick={() => setPeriod(p.value)}>
              {p.label}
            </Button>
          ))}
        </div>
      </PageHeader>
      {data && (
        <p className="text-sm text-muted">
          {data.from ? `Dal ${formatDay(data.from)} al ${formatDay(data.to)}.` : `Tutto lo storico fino al ${formatDay(data.to)}.`}{" "}
          {data.last_read_at ? `Costi letti da Paperclip il ${formatDateTime(data.last_read_at)}.` : "Paperclip non è ancora stato letto: i costi sono a zero."}{" "}
          {`Aggiornato ${formatDateTime(data.as_of)}.`}
        </p>
      )}
      {isError && <p className="text-loss">Impossibile caricare i risultati degli agenti</p>}
      {isLoading && <p className="text-sm text-muted">Caricamento…</p>}
      {data && <TeamCard team={data.team} />}
      <Card role="region" aria-label="Per agente">
        <CardHeader>
          <CardTitle>Per agente</CardTitle>
        </CardHeader>
        <CardContent>
          <DataTable aria-label="Risultati per agente" columns={agentColumns} rows={data?.agents} rowKey={(r) => r.agent_id} loading={isLoading} empty="Nessun agente" />
        </CardContent>
      </Card>
      <Card role="region" aria-label="Per strategia">
        <CardHeader>
          <CardTitle>Per strategia</CardTitle>
        </CardHeader>
        <CardContent>
          <DataTable
            aria-label="Risultati per strategia"
            columns={strategyColumns}
            rows={data?.strategies}
            rowKey={(r) => r.ref ?? "none"}
            loading={isLoading}
            empty="Nessuna operazione nel periodo"
          />
        </CardContent>
      </Card>
      <div className="space-y-1 text-xs text-muted">
        <p>
          Risultato in euro delle operazioni chiuse nel periodo (le uscite chiudono le entrate più vecchie), al netto di commissioni e cambio, al cambio di oggi; le posizioni ancora aperte non
          contano. Una proposta è approvata o respinta dall&apos;ultima approvazione o dall&apos;ultimo veto con lo stesso riferimento.
        </p>
        <p>
          Costo agenti in dollari come lo riporta Paperclip: a consumo API, incluso nell&apos;abbonamento e altro; «fatturato» esclude quanto è incluso nell&apos;abbonamento. Le lezioni riguardano un
          agente quando lo nominano tra i ruoli coinvolti (nome o sigla, es. «PM»).
        </p>
      </div>
    </div>
  );
}
