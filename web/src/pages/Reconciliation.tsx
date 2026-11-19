import { RefreshCw, Scale } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";
import { useIsAdmin } from "@/api/hooks/auth";
import { useReconciliation, useReconciliationDetail, useRunReconciliation } from "@/api/hooks/reconciliation";
import type { CheckItem, CheckStatus, ReconciliationCheck, ReconciliationPortfolio } from "@/api/types";
import { DataTable, type Column } from "@/components/DataTable";
import { PageHeader } from "@/components/PageHeader";
import { StatusBadge, type StatusTone } from "@/components/StatusBadge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { errorMessage } from "@/lib/errors";
import { formatDateTime, formatQty } from "@/lib/format";
import { cn } from "@/lib/utils";

const CHECK_STATUS: Record<CheckStatus, { label: string; tone: StatusTone }> = {
  ok: { label: "Allineato", tone: "ok" },
  warning: { label: "Da verificare", tone: "warn" },
  mismatch: { label: "Differenze", tone: "danger" },
  unverifiable: { label: "Non verificabile", tone: "neutral" },
};

const ITEM_STATUS: Record<CheckStatus, { label: string; tone: StatusTone }> = {
  ok: { label: "Uguale", tone: "ok" },
  warning: { label: "Da verificare", tone: "warn" },
  mismatch: { label: "Diverso", tone: "danger" },
  unverifiable: { label: "Non verificabile", tone: "neutral" },
};

const KIND_LABEL: Record<CheckItem["kind"], string> = { position: "Posizione", cash: "Liquidità", order: "Ordine aperto" };

function PortfolioStatus({ p }: { p: ReconciliationPortfolio }) {
  if (!p.linked) return <StatusBadge tone="neutral">Non collegato</StatusBadge>;
  if (!p.latest) return <StatusBadge tone="neutral">Mai controllato</StatusBadge>;
  const s = CHECK_STATUS[p.latest.status];
  return <StatusBadge tone={s.tone}>{s.label}</StatusBadge>;
}

const qty = (v: string | null) => (v == null ? "—" : formatQty(v));

function StatusCard({
  p,
  selected,
  onSelect,
  onRun,
  running,
  admin,
}: {
  p: ReconciliationPortfolio;
  selected: boolean;
  onSelect: () => void;
  onRun: () => void;
  running: boolean;
  admin: boolean;
}) {
  const c = p.latest?.counts;
  return (
    <Card className={cn(selected && "ring-2 ring-accent")} aria-label={`Riconciliazione ${p.portfolio_name}`} role="region">
      <CardHeader className="pb-2">
        <div className="flex items-start justify-between gap-2">
          <CardTitle className="text-base">{p.portfolio_name}</CardTitle>
          <PortfolioStatus p={p} />
        </div>
      </CardHeader>
      <CardContent className="space-y-2 text-sm">
        {!p.linked ? (
          <p className="text-muted">Nessun broker collegato: niente da confrontare.</p>
        ) : (
          <>
            <p className="text-muted">
              {p.link?.name}
              {p.link?.live ? " · denaro reale" : " · conto di prova"}
              {p.link && !p.link.enabled ? " · collegamento disattivato" : ""}
            </p>
            <p>
              Ultimo controllo: <span className="num">{p.latest ? formatDateTime(p.latest.ts) : "—"}</span>
            </p>
            {p.latest?.status === "unverifiable" && (
              <p className="text-muted">
                Ultima verifica riuscita: <span className="num">{p.last_verified_at ? formatDateTime(p.last_verified_at) : "—"}</span>
              </p>
            )}
            {c && p.latest?.status !== "unverifiable" && (
              <p className="text-muted">
                {c.ok} uguali · {c.warning + c.unverifiable} da verificare · {c.mismatch} diversi
              </p>
            )}
            <div className="flex flex-wrap gap-2 pt-1">
              <Button size="sm" variant={selected ? "default" : "outline"} onClick={onSelect} aria-pressed={selected}>
                Dettagli
              </Button>
              {admin && p.link?.enabled && (
                <Button size="sm" variant="outline" onClick={onRun} disabled={running}>
                  <RefreshCw className={cn(running && "animate-spin")} />
                  Riconcilia ora
                </Button>
              )}
            </div>
          </>
        )}
      </CardContent>
    </Card>
  );
}

function ItemsTable({ check }: { check: ReconciliationCheck }) {
  const ccy = check.meta.currency ?? "";
  const columns: Column<CheckItem>[] = [
    {
      key: "label",
      header: "Voce",
      cell: (i) => (
        <span>
          <span className="block font-medium">{i.label}</span>
          <span className="block text-xs text-muted">{KIND_LABEL[i.kind]}</span>
        </span>
      ),
    },
    { key: "ours", header: "SMTrading", align: "right", cell: (i) => <span className="num">{qty(i.ours)}</span> },
    { key: "broker", header: "Broker", align: "right", cell: (i) => <span className="num">{qty(i.broker)}</span> },
    {
      key: "diff",
      header: "Differenza",
      align: "right",
      cell: (i) => <span className={cn("num", i.diff && i.diff !== "0" && i.status !== "ok" && "text-loss")}>{qty(i.diff)}</span>,
    },
    {
      key: "status",
      header: "Stato",
      cell: (i) => <StatusBadge tone={ITEM_STATUS[i.status].tone}>{ITEM_STATUS[i.status].label}</StatusBadge>,
      sortValue: (i) => ["mismatch", "warning", "unverifiable", "ok"].indexOf(i.status),
    },
    {
      key: "note",
      header: "Nota",
      cell: (i) => (
        <span className="text-sm">
          {i.note}
          {i.streak && i.streak > 1 ? ` · da ${i.streak} controlli` : ""}
          {i.alerted ? " · avviso inviato" : ""}
        </span>
      ),
    },
  ];
  return (
    <div className="space-y-2">
      {ccy && <p className="text-xs text-muted">Liquidità nella valuta del conto ({ccy}); quantità in unità.</p>}
      {check.meta.orders_checked === false && (
        <p className="text-sm text-muted">Ordini aperti non confrontati: questo broker non ne espone l'elenco.</p>
      )}
      <DataTable aria-label="Voci confrontate" columns={columns} rows={check.items} rowKey={(i) => i.key} empty="Niente da confrontare" />
    </div>
  );
}

const TRIGGER_LABEL = { schedule: "Automatico", manual: "Manuale" } as const;

function Detail({ portfolioId }: { portfolioId: string }) {
  const q = useReconciliationDetail(portfolioId);
  const d = q.data;
  const history: Column<ReconciliationCheck>[] = [
    { key: "ts", header: "Data", cell: (r) => <span className="num">{formatDateTime(r.ts)}</span> },
    { key: "status", header: "Esito", cell: (r) => <StatusBadge tone={CHECK_STATUS[r.status].tone}>{CHECK_STATUS[r.status].label}</StatusBadge> },
    { key: "trigger", header: "Origine", cell: (r) => TRIGGER_LABEL[r.trigger] },
    { key: "mismatch", header: "Diversi", align: "right", cell: (r) => <span className="num">{r.status === "unverifiable" ? "—" : r.counts.mismatch}</span> },
    {
      key: "error",
      header: "Errore",
      cell: (r) => (
        <span title={r.error ?? undefined} className="block max-w-xs truncate text-xs">
          {r.error ?? "—"}
        </span>
      ),
    },
  ];
  return (
    <Card className="mt-6">
      <CardHeader>
        <CardTitle className="text-base">{d ? `Dettaglio · ${d.portfolio_name}` : "Dettaglio"}</CardTitle>
      </CardHeader>
      <CardContent className="space-y-6">
        {q.isError && <p className="text-sm text-loss">{errorMessage(q.error, "Impossibile caricare il dettaglio")}</p>}
        {q.isLoading && <p className="text-sm text-muted">Caricamento…</p>}
        {d && !d.latest && <p className="text-sm text-muted">Nessun controllo ancora: il primo arriva entro 15 minuti in seduta.</p>}
        {d?.latest?.status === "unverifiable" && (
          <p role="alert" className="rounded-md border border-border p-3 text-sm">
            Il broker non ha risposto all'ultimo controllo ({formatDateTime(d.latest.ts)}): {d.latest.error ?? "errore sconosciuto"}. Nessun
            avviso inviato; il controllo successivo riprova.
          </p>
        )}
        {d?.latest && d.latest.status !== "unverifiable" && <ItemsTable check={d.latest} />}
        {d && (
          <div className="space-y-2">
            <h2 className="text-sm font-semibold">Storico</h2>
            <DataTable aria-label="Storico dei controlli" columns={history} rows={d.history} rowKey={(r) => String(r.id)} empty="Nessun controllo" />
          </div>
        )}
      </CardContent>
    </Card>
  );
}

/** «Riconciliazione»: SMTrading against the broker account, portfolio by portfolio. */
export default function Reconciliation() {
  const admin = useIsAdmin();
  const list = useReconciliation();
  const run = useRunReconciliation();
  const [chosen, setChosen] = useState<string | null>(null);
  const rows = list.data;
  const firstLinked = rows?.find((p) => p.linked)?.portfolio_id ?? null;
  const selected = chosen ?? firstLinked;
  const runningFor = run.isPending ? (run.variables ?? "all") : null;

  function runNow(portfolioId: string | null) {
    run.mutate(portfolioId, {
      onSuccess: () => toast.success("Riconciliazione eseguita"),
      onError: (e) => toast.error(errorMessage(e, "Riconciliazione non riuscita")),
    });
  }

  return (
    <div>
      <PageHeader title="Riconciliazione">
        {admin && rows?.some((p) => p.linked && p.link?.enabled) && (
          <Button onClick={() => runNow(null)} disabled={run.isPending}>
            <RefreshCw className={cn(runningFor === "all" && "animate-spin")} />
            Riconcilia ora
          </Button>
        )}
      </PageHeader>
      <p className="mb-4 flex items-start gap-2 text-sm text-muted">
        <Scale className="mt-0.5 h-4 w-4 shrink-0" aria-hidden />
        Posizioni, liquidità e ordini aperti di ogni portafoglio collegato, confrontati con il conto del broker ogni 15 minuti durante le
        sedute e una volta dopo la chiusura USA. Solo letture: nessun ordine viene inviato o annullato e nulla si corregge da sé. Una
        differenza che resta per 2 controlli di fila avvisa su Telegram e gli agenti del portafoglio.
      </p>
      {list.isError && <p className="text-sm text-loss">{errorMessage(list.error, "Impossibile caricare la riconciliazione")}</p>}
      {list.isLoading && <p className="text-sm text-muted">Caricamento…</p>}
      {rows && rows.length === 0 && <p className="text-sm text-muted">Nessun portafoglio</p>}
      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
        {rows?.map((p) => (
          <StatusCard
            key={p.portfolio_id}
            p={p}
            admin={admin}
            selected={p.portfolio_id === selected}
            onSelect={() => setChosen(p.portfolio_id)}
            onRun={() => runNow(p.portfolio_id)}
            running={runningFor === p.portfolio_id || runningFor === "all"}
          />
        ))}
      </div>
      {selected && <Detail portfolioId={selected} />}
    </div>
  );
}
