import { useMemo, useState } from "react";
import { toast } from "sonner";
import { useAutoRuleAction, useAutoRuleModels, useAutoRuleResults, useAutoRules, useAutoRuleTrades, usePurgeAutoRules, type AutoRuleActionName } from "@/api/hooks/autoRules";
import { useIsAdmin } from "@/api/hooks/auth";
import { usePortfolios } from "@/api/hooks/portfolios";
import type { AutoRule, AutoRuleResult, AutoRuleStream, AutoTrade } from "@/api/types";
import { AutoRuleBacktest } from "@/components/AutoRuleBacktest";
import { AutoRuleForm } from "@/components/AutoRuleForm";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { DataTable, type Column } from "@/components/DataTable";
import { FormField, selectClass } from "@/components/FormField";
import { Money } from "@/components/Money";
import { PageHeader } from "@/components/PageHeader";
import { Pnl } from "@/components/Pnl";
import { StatusBadge, type StatusTone } from "@/components/StatusBadge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { BLOCKED_LABELS, EXIT_REASON_LABELS, MODEL_LABELS, TRADE_STATUS_LABELS } from "@/lib/autoRules";
import { errorMessage } from "@/lib/errors";
import { formatDateTime, formatMoney, formatQty } from "@/lib/format";

const INTRO = "Prove: le regole lavorano solo sui portafogli con l'interruttore acceso. Il kill switch ferma tutto.";
const STATUS_TONE: Record<string, StatusTone> = { draft: "neutral", active: "ok", paused: "warn", braked: "danger" };
const STREAM_LABELS: Record<AutoRuleStream, string> = { stream: "con flusso", polling: "in polling", none: "senza flusso" };
const STREAM_TONE: Record<AutoRuleStream, StatusTone> = { stream: "ok", polling: "info", none: "warn" };
const STREAM_TITLE: Record<AutoRuleStream, string> = {
  stream: "I prezzi arrivano in tempo reale",
  polling: "I prezzi si leggono a intervalli: la regola non entra",
  none: "Nessun flusso di prezzi per questo simbolo: la regola non entra",
};

const ms = (v: number | string | null | undefined) => (v == null ? "—" : `${formatQty(String(v))} ms`);

/** Attiva / Metti in pausa / Riattiva, and Modifica for a rule that is not active; the server's refusal shows under them. */
function RuleActions({ rule, onEdit }: { rule: AutoRule; onEdit: (r: AutoRule) => void }) {
  const activate = useAutoRuleAction(rule.id, "activate");
  const pause = useAutoRuleAction(rule.id, "pause");
  const resume = useAutoRuleAction(rule.id, "resume");
  const all = [activate, pause, resume];
  const busy = all.some((m) => m.isPending);
  const error = all.find((m) => m.error)?.error;
  const run = (m: typeof activate, what: AutoRuleActionName) => {
    all.forEach((x) => x.reset());
    m.mutate(undefined, { onSuccess: () => toast.success(what === "pause" ? "Regola in pausa" : what === "activate" ? "Regola attivata" : "Regola riattivata") });
  };
  return (
    <div className="space-y-1">
      <div className="flex flex-wrap gap-2">
        {rule.status === "draft" && (
          <Button size="sm" disabled={busy} onClick={() => run(activate, "activate")}>
            Attiva
          </Button>
        )}
        {rule.status === "active" && (
          <Button size="sm" variant="outline" disabled={busy} onClick={() => run(pause, "pause")}>
            Metti in pausa
          </Button>
        )}
        {(rule.status === "paused" || rule.status === "braked") && (
          <Button size="sm" disabled={busy} onClick={() => run(resume, "resume")}>
            Riattiva
          </Button>
        )}
        {rule.status !== "active" && (
          <Button size="sm" variant="outline" disabled={busy} onClick={() => onEdit(rule)}>
            Modifica
          </Button>
        )}
      </div>
      {error && (
        <p role="alert" className="text-xs text-loss">
          {errorMessage(error)}
        </p>
      )}
    </div>
  );
}

function ruleColumns(onEdit: (r: AutoRule) => void): Column<AutoRule>[] {
  return [
    { key: "symbol", header: "Simbolo", primary: true, sortValue: (r) => r.symbol, cell: (r) => <span className="font-medium">{r.symbol}</span> },
    { key: "portfolio", header: "Portafoglio", sortValue: (r) => r.portfolio_name, cell: (r) => r.portfolio_name },
    { key: "model", header: "Modello", cell: (r) => `${MODEL_LABELS[r.model] ?? r.model} · versione ${r.version}` },
    {
      key: "status",
      header: "Stato",
      cell: (r) => (
        <div className="space-y-1">
          <StatusBadge tone={STATUS_TONE[r.status] ?? "neutral"}>{r.status_it}</StatusBadge>
          {r.status_reason && <p className="text-xs text-muted">{r.status_reason}</p>}
          {r.open_trade && <p className="text-xs text-muted">operazione aperta</p>}
        </div>
      ),
    },
    {
      key: "stream",
      header: "Prezzi",
      cell: (r) => (
        <StatusBadge tone={STREAM_TONE[r.stream] ?? "neutral"} title={STREAM_TITLE[r.stream]}>
          {STREAM_LABELS[r.stream] ?? r.stream_it}
        </StatusBadge>
      ),
    },
    {
      key: "today",
      header: "Oggi (giorno del portafoglio)",
      align: "right",
      cell: (r) => (
        <span className="whitespace-nowrap">
          <span className="num">{r.today.trades}</span> op. · <Pnl value={r.today.net} currency={r.today.net_currency} />
        </span>
      ),
    },
    { key: "actions", header: "", cell: (r) => <RuleActions rule={r} onEdit={onEdit} /> },
  ];
}

/** End of a drill: deletes every rule and trade of a test portfolio (the server refuses campaign
 * portfolios, portfolios with agents, active rules and open trades). */
function PurgeDialog({ portfolioId, name, onClose }: { portfolioId: string; name: string; onClose: () => void }) {
  const purge = usePurgeAutoRules(portfolioId);
  return (
    <ConfirmDialog
      open
      onOpenChange={(o) => !o && onClose()}
      title={`Cancellare le regole di ${name}?`}
      description="Si cancellano tutte le regole del portafoglio, le loro versioni e le loro operazioni registrate. Non si può annullare."
      confirmLabel="Cancella tutto"
      destructive
      pending={purge.isPending}
      error={purge.error ? errorMessage(purge.error) : null}
      onConfirm={() =>
        purge.mutate(undefined, {
          onSuccess: (out) => {
            toast.success(`Cancellate ${String(out.rules ?? 0)} regole e ${String(out.trades ?? 0)} operazioni`);
            onClose();
          },
        })
      }
    />
  );
}

function PurgeSection() {
  const portfolios = usePortfolios();
  const [id, setId] = useState("");
  const [open, setOpen] = useState(false);
  const chosen = portfolios.data?.find((p) => p.id === id);
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Fine della prova</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        <p className="text-sm text-muted">Cancella le regole e le operazioni di un portafoglio di prova senza agenti. Con regole attive o operazioni aperte il server rifiuta.</p>
        <div className="flex flex-wrap items-end gap-3">
          <div className="min-w-48">
            <FormField label="Portafoglio da svuotare">
              <select className={selectClass} value={id} onChange={(e) => setId(e.target.value)}>
                <option value="">scegli…</option>
                {(portfolios.data ?? []).map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.name}
                  </option>
                ))}
              </select>
            </FormField>
          </div>
          <Button variant="outline" disabled={!chosen} onClick={() => setOpen(true)}>
            Cancella le regole del portafoglio
          </Button>
        </div>
        {open && chosen && <PurgeDialog portfolioId={chosen.id} name={chosen.name} onClose={() => setOpen(false)} />}
      </CardContent>
    </Card>
  );
}

const feesText = (fees: Record<string, string>) => {
  const parts = Object.entries(fees).map(([cur, v]) => formatMoney(v, cur));
  return parts.length ? parts.join(" + ") : "—";
};

function resultColumns(label: (id: string) => string): Column<AutoRuleResult>[] {
  return [
    { key: "day", header: "Giorno (UTC)", primary: true, sortValue: (r) => r.day, cell: (r) => <span className="num">{r.day.split("-").reverse().join("/")}</span> },
    { key: "rule", header: "Regola", sortValue: (r) => label(r.rule_id), cell: (r) => `${label(r.rule_id)} · v${r.version}` },
    { key: "trades", header: "Operazioni", align: "right", sortValue: (r) => r.trades, cell: (r) => <span className="num">{r.trades}</span> },
    { key: "wl", header: "Vinte / perse", align: "right", cell: (r) => <span className="num">{r.wins} / {r.losses}</span> },
    { key: "net", header: "Netto", align: "right", sortValue: (r) => Number(r.net), cell: (r) => <Pnl value={r.net} currency={r.net_currency ?? "EUR"} /> },
    { key: "fees", header: "Commissioni", align: "right", cell: (r) => <span className="num whitespace-nowrap">{feesText(r.fees)}</span> },
    { key: "t1", header: "Scatto → invio", align: "right", cell: (r) => <span className="num">{ms(r.median_ms_trigger_to_send)}</span> },
    { key: "t2", header: "Invio → broker", align: "right", cell: (r) => <span className="num">{ms(r.median_ms_send_to_broker)}</span> },
    { key: "t3", header: "Broker → riempimento", align: "right", cell: (r) => <span className="num">{ms(r.median_ms_broker_to_fill)}</span> },
    { key: "slip", header: "Scarto medio in entrata", align: "right", cell: (r) => <span className="num">{r.avg_entry_slippage_bps == null ? "—" : `${formatQty(r.avg_entry_slippage_bps)} pb`}</span> },
    {
      key: "blocked",
      header: "Scatti bloccati",
      cell: (r) => {
        const e = Object.entries(r.blocked);
        return e.length ? e.map(([k, n]) => `${BLOCKED_LABELS[k] ?? k}: ${n}`).join(", ") : "—";
      },
    },
  ];
}

function Results({ portfolioId, rules }: { portfolioId: string; rules: AutoRule[] }) {
  const results = useAutoRuleResults(portfolioId ? { portfolio_id: portfolioId } : {});
  const label = (id: string) => {
    const r = rules.find((x) => x.id === id);
    return r ? `${r.portfolio_name} · ${r.symbol}` : "regola cancellata";
  };
  const rows = useMemo(() => [...(results.data ?? [])].sort((a, b) => b.day.localeCompare(a.day)), [results.data]);
  return (
    <div className="space-y-3">
      <p className="text-sm text-muted">Un risultato per regola e per giorno UTC (gli ultimi 30). Le cifre «Oggi» dell'elenco seguono invece il giorno locale del portafoglio, come i freni. Tempi: mediana in millisecondi.</p>
      {results.error && (
        <p role="alert" className="text-sm text-loss">
          {errorMessage(results.error)}
        </p>
      )}
      <DataTable aria-label="Risultati per regola e per giorno" columns={resultColumns(label)} rows={results.error ? [] : rows} rowKey={(r) => `${r.rule_id}-${r.day}`} loading={results.isLoading} empty="Nessun risultato negli ultimi 30 giorni" />
    </div>
  );
}

const tradeColumns: Column<AutoTrade>[] = [
  { key: "trigger", header: "Scatto", primary: true, cell: (t) => formatDateTime(t.trigger_at) },
  { key: "status", header: "Stato", cell: (t) => TRADE_STATUS_LABELS[t.status] ?? t.status },
  { key: "qty", header: "Pezzi", align: "right", cell: (t) => <span className="num">{t.qty == null ? "—" : formatQty(t.qty)}</span> },
  { key: "expected", header: "Prezzo atteso", align: "right", cell: (t) => <span className="num">{formatQty(t.expected_price)}</span> },
  { key: "fill", header: "Prezzo eseguito", align: "right", cell: (t) => <span className="num">{t.fill_price == null ? "—" : formatQty(t.fill_price)}</span> },
  { key: "exit_at", header: "Uscita", cell: (t) => (t.exit_filled_at ? formatDateTime(t.exit_filled_at) : "—") },
  { key: "exit_expected", header: "Uscita attesa", align: "right", cell: (t) => <span className="num">{t.exit_expected == null ? "—" : formatQty(t.exit_expected)}</span> },
  { key: "exit_price", header: "Uscita eseguita", align: "right", cell: (t) => <span className="num">{t.exit_price == null ? "—" : formatQty(t.exit_price)}</span> },
  { key: "reason", header: "Motivo d'uscita", cell: (t) => (t.exit_reason ? (EXIT_REASON_LABELS[t.exit_reason] ?? t.exit_reason) : "—") },
  { key: "m1", header: "Scatto → invio", align: "right", cell: (t) => <span className="num">{ms(t.ms_trigger_to_send)}</span> },
  { key: "m2", header: "Invio → broker", align: "right", cell: (t) => <span className="num">{ms(t.ms_send_to_broker)}</span> },
  { key: "m3", header: "Broker → riempimento", align: "right", cell: (t) => <span className="num">{ms(t.ms_broker_to_fill)}</span> },
  { key: "fees", header: "Commissioni", align: "right", cell: (t) => <Money value={t.fees_currency ? t.fees : null} currency={t.fees_currency ?? "EUR"} /> },
  { key: "net", header: "Netto", align: "right", cell: (t) => <Pnl value={t.net_result} currency={t.net_currency ?? "EUR"} /> },
  { key: "error", header: "Errore", hideOnMobile: false, cell: (t) => (t.error ? <span className="text-xs text-loss">{t.error}</span> : <span className="text-muted">—</span>) },
];

function Trades({ rules }: { rules: AutoRule[] }) {
  const [id, setId] = useState("");
  const current = rules.find((r) => r.id === id) ?? rules[0];
  const trades = useAutoRuleTrades(current?.id ?? null);
  if (rules.length === 0) return <p className="text-sm text-muted">Nessuna regola: le operazioni compaiono qui.</p>;
  return (
    <div className="space-y-3">
      <div className="max-w-md">
        <FormField label="Regola">
          <select className={selectClass} value={current?.id ?? ""} onChange={(e) => setId(e.target.value)}>
            {rules.map((r) => (
              <option key={r.id} value={r.id}>
                {r.portfolio_name} · {r.symbol} · {MODEL_LABELS[r.model] ?? r.model}
              </option>
            ))}
          </select>
        </FormField>
      </div>
      {trades.error && (
        <p role="alert" className="text-sm text-loss">
          {errorMessage(trades.error)}
        </p>
      )}
      <DataTable aria-label="Operazioni della regola" columns={tradeColumns} rows={trades.error ? [] : trades.data} rowKey={(t) => String(t.id)} loading={trades.isLoading} empty="Nessuna operazione per questa regola" />
    </div>
  );
}

function RulesAdmin() {
  const models = useAutoRuleModels();
  const portfolios = usePortfolios();
  const [portfolioId, setPortfolioId] = useState("");
  const rules = useAutoRules(portfolioId || undefined);
  // «new» opens the empty form, a rule opens its edit form
  const [editing, setEditing] = useState<AutoRule | "new" | null>(null);
  const list = rules.data ?? [];
  return (
    <>
      <PageHeader title="Regole automatiche">
        <Button onClick={() => setEditing("new")} disabled={!models.data || editing !== null}>
          Nuova regola
        </Button>
      </PageHeader>
      <p className="mb-4 text-sm text-muted">{INTRO}</p>
      {editing !== null && models.data && (
        <div className="mb-6">
          <AutoRuleForm key={editing === "new" ? "new" : `${editing.id}-${editing.version}`} models={models.data} rule={editing === "new" ? undefined : editing} onDone={() => setEditing(null)} />
        </div>
      )}
      {models.error && (
        <p role="alert" className="mb-4 text-sm text-loss">
          {errorMessage(models.error)}
        </p>
      )}
      <div className="mb-4 max-w-xs">
        <FormField label="Mostra il portafoglio">
          <select className={selectClass} value={portfolioId} onChange={(e) => setPortfolioId(e.target.value)}>
            <option value="">tutti</option>
            {(portfolios.data ?? []).map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
              </option>
            ))}
          </select>
        </FormField>
      </div>
      <Tabs defaultValue="rules">
        <TabsList className="mb-4 flex-wrap">
          <TabsTrigger value="rules">Regole</TabsTrigger>
          <TabsTrigger value="results">Risultati</TabsTrigger>
          <TabsTrigger value="trades">Operazioni</TabsTrigger>
          <TabsTrigger value="backtest">Prova sullo storico</TabsTrigger>
        </TabsList>
        <TabsContent value="rules" className="space-y-6">
          {rules.error && (
            <p role="alert" className="text-sm text-loss">
              {errorMessage(rules.error)}
            </p>
          )}
          <DataTable aria-label="Regole automatiche" columns={ruleColumns(setEditing)} rows={rules.error ? [] : rules.data} rowKey={(r) => r.id} loading={rules.isLoading} empty="Nessuna regola: creane una con «Nuova regola»" />
          <PurgeSection />
        </TabsContent>
        <TabsContent value="results">
          <Results portfolioId={portfolioId} rules={list} />
        </TabsContent>
        <TabsContent value="trades">
          <Trades rules={list} />
        </TabsContent>
        <TabsContent value="backtest">
          <AutoRuleBacktest key={list.map((r) => r.id).join(",")} rules={list} />
        </TabsContent>
      </Tabs>
    </>
  );
}

export default function AutoRules() {
  const admin = useIsAdmin();
  if (!admin) {
    return (
      <>
        <PageHeader title="Regole automatiche" />
        <p className="text-sm text-muted">Questa pagina è riservata agli amministratori.</p>
      </>
    );
  }
  return <RulesAdmin />;
}
