import { useState } from "react";
import { useModelCompare } from "@/api/hooks/paperclip";
import type { AgentCostModels, ModelCompare, ModelCostRow } from "@/api/types";
import { DataTable, type Column } from "@/components/DataTable";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { formatDateTime, formatPct } from "@/lib/format";
import { formatUsd } from "@/lib/paperclip";
import { cn } from "@/lib/utils";

const usd = (v: string | null) => (v == null ? "—" : formatUsd(v));

const modelColumns: Column<ModelCostRow>[] = [
  { key: "model", header: "Modello", primary: true, sortValue: (r) => r.model, cell: (r) => <span className="font-mono text-sm">{r.model}</span> },
  { key: "billed", header: "Spesa ($)", align: "right", sortValue: (r) => Number(r.billed_usd), cell: (r) => <span className="num">{usd(r.billed_usd)}</span> },
  {
    key: "runs",
    header: "Esecuzioni",
    align: "right",
    sortValue: (r) => r.runs,
    cell: (r) => (
      <span className="num" title={r.derived_runs ? `${r.derived_runs} con il modello ricavato dalla configurazione dell'agente` : undefined}>
        {r.runs}
        {r.derived_runs > 0 && "*"}
      </span>
    ),
  },
  {
    key: "per_run",
    header: "Costo per esecuzione",
    align: "right",
    sortValue: (r) => Number(r.billed_per_run_usd ?? 0),
    cell: (r) => <span className="num">{usd(r.billed_per_run_usd)}</span>,
  },
  {
    key: "full_per_run",
    header: "Con abbonamento",
    mobileLabel: "Per esecuzione, con abbonamento",
    align: "right",
    sortValue: (r) => Number(r.full_per_run_usd ?? 0),
    cell: (r) => <span className="num text-muted">{usd(r.full_per_run_usd)}</span>,
  },
  { key: "agents", header: "Agenti", cell: (r) => <span className="text-sm">{r.agents.join(", ") || "—"}</span> },
];

function totals(rows: ModelCostRow[]): { billed: number; runs: number; perRun: number | null } {
  const billed = rows.reduce((s, r) => s + Number(r.billed_usd), 0);
  const runs = rows.reduce((s, r) => s + r.runs, 0);
  return { billed, runs, perRun: runs ? billed / runs : null };
}

const ROME = new Intl.DateTimeFormat("it-IT", {
  timeZone: "Europe/Rome",
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  hourCycle: "h23",
});

/** ISO → the `datetime-local` value in Rome time («2026-11-19T09:38»). */
export function romeInputValue(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const p = Object.fromEntries(ROME.formatToParts(d).map((x) => [x.type, x.value]));
  return `${p.year}-${p.month}-${p.day}T${p.hour}:${p.minute}`;
}

function Side({ title, side }: { title: string; side: ModelCompare["before"] }) {
  const t = totals(side.models);
  return (
    <div className="min-w-0 space-y-2" role="group" aria-label={title}>
      <h3 className="text-sm font-medium">{title}</h3>
      <p className="text-xs text-muted">{`${formatDateTime(side.from.length === 10 ? `${side.from}T00:00:00` : side.from)} – ${formatDateTime(side.to)}`}</p>
      <p className="text-sm">
        <span className="num">{formatUsd(t.billed.toFixed(4))}</span> in <span className="num">{t.runs}</span> esecuzioni ·{" "}
        <span className="num font-medium">{t.perRun == null ? "—" : formatUsd(t.perRun.toFixed(4))}</span> per esecuzione
      </p>
      <DataTable aria-label={`Modelli ${title.toLowerCase()}`} columns={modelColumns.slice(0, 4)} rows={side.models} rowKey={(r) => r.model} empty="Nessuna esecuzione" />
    </div>
  );
}

/** «Prima e dopo»: per model, the days before a time (a model change) against after it. */
function CompareCard({ models }: { models: AgentCostModels }) {
  const last = models.changes[0]?.at ?? null;
  const [at, setAt] = useState<string>(() => (last ? romeInputValue(last) : ""));
  const [days, setDays] = useState(7);
  const q = useModelCompare(at || null, days, Boolean(at));
  const data = q.data;
  const before = data ? totals(data.before.models) : null;
  const after = data ? totals(data.after.models) : null;
  const delta = before?.perRun && after?.perRun != null ? ((after.perRun - before.perRun) / before.perRun) * 100 : null;
  return (
    <Card role="region" aria-label="Prima e dopo un cambio">
      <CardHeader>
        <CardTitle>Prima e dopo un cambio</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        {models.changes.length > 0 && (
          <div className="flex flex-wrap gap-2" role="group" aria-label="Cambi di modello">
            {models.changes.slice(0, 5).map((c) => (
              <button
                key={`${c.agent_id}-${c.at}`}
                type="button"
                onClick={() => setAt(romeInputValue(c.at))}
                className={cn("min-h-9 rounded-md border border-border px-2 py-1 text-left text-xs hover:bg-card", romeInputValue(c.at) === at && "border-accent")}
              >
                <span className="font-medium">{c.name}</span>: <span className="font-mono">{c.from}</span> → <span className="font-mono">{c.to}</span>
                <span className="block text-muted">{formatDateTime(c.at)}</span>
              </button>
            ))}
          </div>
        )}
        <div className="flex flex-wrap items-end gap-3 text-sm">
          <label className="flex flex-col gap-1">
            <span className="text-muted">Data e ora del cambio</span>
            <input type="datetime-local" className="h-10 rounded-md border border-border bg-bg px-2" value={at} onChange={(e) => setAt(e.target.value)} />
          </label>
          <label className="flex flex-col gap-1">
            <span className="text-muted">Giorni da confrontare</span>
            <select className="h-10 rounded-md border border-border bg-bg px-2" value={days} onChange={(e) => setDays(Number(e.target.value))}>
              {[3, 7, 14, 30].map((d) => (
                <option key={d} value={d}>
                  {d}
                </option>
              ))}
            </select>
          </label>
        </div>
        {!at && <p className="text-sm text-muted">Nessun cambio di modello registrato: indica data e ora.</p>}
        {q.isError && <p className="text-sm text-loss">{(q.error as Error)?.message || "Confronto non disponibile"}</p>}
        {data && (
          <>
            {delta != null && (
              <p className="text-sm" role="status">
                Costo per esecuzione dopo il cambio: <span className={cn("num font-medium", delta > 0 ? "text-loss" : "text-gain")}>{formatPct(delta.toFixed(2))}</span>
              </p>
            )}
            <div className="grid gap-4 md:grid-cols-2">
              <Side title="Prima" side={data.before} />
              <Side title="Dopo" side={data.after} />
            </div>
            {data.straddling.length > 0 && (
              <p className="text-xs text-muted">
                Esclusi (esecuzioni del giorno sia prima sia dopo l&apos;ora indicata):{" "}
                {data.straddling.map((s) => `${s.agent} · ${s.model} (${s.runs})`).join(", ")}.
              </p>
            )}
          </>
        )}
      </CardContent>
    </Card>
  );
}

/** «Per modello»: spend, runs and cost per run of each model in the period, every agent's
 * model now and the before/after view around a model change. */
export function AgentModelCosts({ models }: { models: AgentCostModels }) {
  const derived = models.rows.some((r) => r.derived_runs > 0);
  return (
    <>
      <Card role="region" aria-label="Per modello">
        <CardHeader>
          <CardTitle>Per modello</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          <DataTable aria-label="Costi per modello" columns={modelColumns} rows={models.rows} rowKey={(r) => r.model} empty="Nessuna esecuzione nel periodo" />
          {models.agents.length > 0 && (
            <div>
              <h3 className="mb-1 text-sm font-medium">Modello di ogni agente ora</h3>
              <ul className="grid gap-1 text-sm sm:grid-cols-2" aria-label="Modello di ogni agente">
                {models.agents.map((a) => (
                  <li key={a.paperclip_agent_id} className="flex flex-wrap items-baseline justify-between gap-2">
                    <span>{a.name}</span>
                    <span className="text-right">
                      <span className="font-mono">{a.model ?? "—"}</span>
                      {a.since && <span className="ml-2 text-xs text-muted">{`dal ${formatDateTime(a.since)}`}</span>}
                    </span>
                  </li>
                ))}
              </ul>
            </div>
          )}
          <p className="text-xs text-muted">
            Il modello di ogni esecuzione è quello che riporta Paperclip; spesa e costo per esecuzione contano solo il fatturato (a consumo API e altro), «con abbonamento» anche il
            valore delle esecuzioni incluse nel piano.
            {derived && " * Alcune esecuzioni non indicano il modello: vale quello configurato per l'agente in quel momento."}
          </p>
        </CardContent>
      </Card>
      <CompareCard models={models} />
    </>
  );
}
