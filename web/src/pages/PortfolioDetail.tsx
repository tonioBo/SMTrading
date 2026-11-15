import { ArchiveRestore, ArrowLeft, Plus, ShieldAlert } from "lucide-react";
import { useState, type ReactNode } from "react";
import { toast } from "sonner";
import { Link, useParams } from "react-router-dom";
import {
  OPEN_ORDER_STATUSES,
  useArchivePortfolio,
  useCancelOrder,
  useLiveQuotes,
  useOrders,
  usePatchPortfolio,
  usePerformance,
  usePortfolio,
  usePositions,
  useRiskProfiles,
  useValuation,
} from "@/api/hooks/portfolios";
import { useLiveFlatten, useLiveLinks, useLiveResume } from "@/api/hooks/live";
import type { LiveState, Order, Portfolio, Position, Valuation } from "@/api/types";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { DataTable, type Column } from "@/components/DataTable";
import { EquityChart } from "@/components/EquityChart";
import { Money } from "@/components/Money";
import { HaltBanner } from "@/components/HaltBanner";
import { LiveDisarmButton } from "@/components/LiveLinkWizard";
import { LiveTotpDialog } from "@/components/LiveTotpDialog";
import { OrderForm } from "@/components/OrderForm";
import { PageHeader } from "@/components/PageHeader";
import { Pnl } from "@/components/Pnl";
import { PortfolioModeBadge } from "@/components/PortfolioModeBadge";
import { PortfolioStatusBadge } from "@/components/PortfolioStatusBadge";
import { selectClass } from "@/components/FormField";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { BROKER_STATUS_LABELS } from "@/lib/broker";
import { subtractDecimal } from "@/lib/decimal";
import { errorMessage } from "@/lib/errors";
import { formatDateTime, formatDecimal, formatMoney, formatPct, formatQty } from "@/lib/format";
import { LIVE_BADGE, LIVE_STATE_LABELS, TRAILING_NOTE, isLiveReason, reduceOnlyReasonLabel } from "@/lib/live";
import { Movements } from "@/pages/PortfolioDetailMovements";
import { Operations } from "@/pages/PortfolioDetailOperations";
import { PortfolioSettings } from "@/pages/PortfolioDetailSettings";

function Kpi({ title, children }: { title: string; children: ReactNode }) {
  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-sm font-medium text-muted">{title}</CardTitle>
      </CardHeader>
      <CardContent className="text-xl font-semibold">{children}</CardContent>
    </Card>
  );
}

function Pct({ value }: { value: string | null }) {
  return <span className="num">{value == null ? "—" : formatPct(value)}</span>;
}

// --- Panoramica ----------------------------------------------------------------------

function Overview({ portfolio }: { portfolio: Portfolio }) {
  const valuation = useValuation(portfolio.id);
  const performance = usePerformance(portfolio.id, "all");
  const cash = Object.entries(valuation.data?.cash ?? {}).sort(([a], [b]) => a.localeCompare(b));

  return (
    <div className="space-y-6">
      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <Kpi title="Equity">
          <Money value={portfolio.equity_eur} />
        </Kpi>
        <Kpi title="P&L del giorno">
          <Pnl value={portfolio.day_pnl_eur} pct={portfolio.day_pnl_pct} />
        </Kpi>
        <Kpi title="Rendimento (totale)">
          <Pct value={performance.data?.return_pct ?? null} />
        </Kpi>
        <Kpi title="Max drawdown">
          <Pct value={performance.data?.max_drawdown_pct ?? null} />
        </Kpi>
        <Kpi title="Sharpe">
          <span className="num">{performance.data?.sharpe ? formatDecimal(performance.data.sharpe, 2) : "—"}</span>
        </Kpi>
        <Kpi title="Esposizione">
          <Money value={valuation.data?.exposure_eur} />
        </Kpi>
        <Kpi title="Potere d'acquisto">
          <Money value={valuation.data?.buying_power_eur} />
        </Kpi>
        <Kpi title="Cash (EUR)">
          <Money value={valuation.data?.cash_eur} />
        </Kpi>
      </div>
      <Card>
        <CardHeader>
          <CardTitle>Equity</CardTitle>
        </CardHeader>
        <CardContent>
          <EquityChart portfolioId={portfolio.id} />
        </CardContent>
      </Card>
      <Card>
        <CardHeader>
          <CardTitle>Cash per valuta</CardTitle>
        </CardHeader>
        <CardContent>
          {valuation.isError ? (
            <p className="text-sm text-loss">{errorMessage(valuation.error, "Valutazione non disponibile")}</p>
          ) : cash.length === 0 ? (
            <p className="text-sm text-muted">{valuation.isLoading ? "Caricamento…" : "Nessun saldo"}</p>
          ) : (
            <ul className="space-y-1 text-sm" aria-label="Cash per valuta">
              {cash.map(([ccy, amount]) => (
                <li key={ccy} className="flex justify-between gap-4">
                  <span>{ccy}</span>
                  <Money value={amount} currency={ccy} />
                </li>
              ))}
            </ul>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

/** Row marker of a real-money portfolio: «🔴 REALE» (or · PROVA / · SOSPESO). */
function LiveMarker({ state }: { state: LiveState | null | undefined }) {
  if (!state) return null;
  return <span className="ml-2 whitespace-nowrap text-xs font-semibold text-loss">🔴 {LIVE_BADGE[state].label}</span>;
}

// --- Posizioni -----------------------------------------------------------------------

function Positions({ portfolioId, liveState }: { portfolioId: string; liveState?: LiveState | null }) {
  const { data, isLoading, isError, error, dataUpdatedAt } = usePositions(portfolioId);
  const live = useLiveQuotes(data?.map((p) => p.symbol) ?? [], dataUpdatedAt);

  const columns: Column<Position>[] = [
    {
      key: "symbol",
      header: "Simbolo",
      sortValue: (p) => p.symbol,
      cell: (p) => (
        <>
          <span className="font-medium">{p.symbol}</span>
          <LiveMarker state={liveState} />
          {p.stale && <span className="ml-2 text-xs text-warn">quotazione non aggiornata</span>}
        </>
      ),
    },
    { key: "side", header: "Lato", cell: (p) => (p.side === "long" ? "Long" : "Short") },
    { key: "qty", header: "Quantità", align: "right", cell: (p) => <span className="num">{formatQty(p.qty)}</span> },
    { key: "avg", header: "Prezzo medio", align: "right", cell: (p) => <Money value={p.avg_price} currency={p.currency} /> },
    {
      key: "price",
      header: "Prezzo",
      align: "right",
      cell: (p) => (
        <span data-testid={`price-${p.symbol}`}>
          <Money value={live[p.symbol] ?? p.mark} currency={p.currency} />
        </span>
      ),
    },
    { key: "value", header: "Valore (EUR)", align: "right", cell: (p) => <Money value={p.market_value_eur} /> },
    {
      key: "pnl",
      header: "P&L non realizzato",
      align: "right",
      cell: (p) => (
        <span className="flex flex-col items-end">
          <Pnl value={p.unrealized_pnl} currency={p.currency} />
          {p.currency !== "EUR" && (
            <span className="text-xs">
              <Pnl value={p.unrealized_pnl_eur} />
            </span>
          )}
        </span>
      ),
    },
  ];

  return (
    <Card>
      <CardContent className="pt-6">
        {isError && <p className="mb-2 text-sm text-loss">{errorMessage(error, "Impossibile caricare le posizioni")}</p>}
        <DataTable
          aria-label="Posizioni"
          columns={columns}
          rows={data}
          rowKey={(p) => p.symbol}
          loading={isLoading}
          empty="Nessuna posizione aperta"
        />
      </CardContent>
    </Card>
  );
}

// --- Ordini --------------------------------------------------------------------------

const ORDER_STATUS_LABELS: Record<string, string> = {
  new: "Nuovo",
  held: "In attesa del padre",
  queued: "In coda",
  pending: "In attesa",
  triggered: "Attivato",
  partially_filled: "Parzialmente eseguito",
  filled: "Eseguito",
  canceled: "Annullato",
  rejected: "Rifiutato",
  expired: "Scaduto",
};

const ORDER_FILTERS = [
  { value: "open", label: "Aperti" },
  { value: "filled", label: "Eseguiti" },
  { value: "canceled", label: "Annullati" },
  { value: "rejected", label: "Rifiutati" },
  { value: "expired", label: "Scaduti" },
  { value: "", label: "Tutti" },
];

function orderPrice(o: Order): string {
  const parts = [];
  if (o.limit_price) parts.push(`lim. ${formatQty(o.limit_price)}`);
  if (o.stop_price) parts.push(`stop ${formatQty(o.stop_price)}`);
  return parts.join(" · ") || "—";
}

/** Where the order lives: at the broker (routed by a linked portfolio) or simulated only. */
function BrokerCell({ order, linked }: { order: Order; linked: boolean }) {
  if (order.broker_link_id) {
    const status = order.broker_status ? (BROKER_STATUS_LABELS[order.broker_status] ?? order.broker_status) : null;
    return (
      <span className="flex flex-col items-start gap-0.5">
        <Badge variant={order.broker_status === "rejected" || order.broker_status === "unknown" ? "destructive" : "default"}>Broker</Badge>
        {status && <span className="text-xs">{status}</span>}
        {order.broker_order_id && <span className="font-mono text-xs text-muted">{order.broker_order_id}</span>}
      </span>
    );
  }
  return linked ? <Badge variant="outline">Solo simulato</Badge> : <span className="text-muted">—</span>;
}

function Orders({ portfolio, readOnly }: { portfolio: Portfolio; readOnly: boolean }) {
  const portfolioId = portfolio.id;
  const linked = Boolean(portfolio.broker_link_id);
  const liveState = portfolio.live_state;
  const real = Boolean(liveState);
  const [ordering, setOrdering] = useState(false);
  const [status, setStatus] = useState("open");
  const { data, isLoading, isError, error } = useOrders(portfolioId, status);
  const cancel = useCancelOrder(portfolioId);
  const [target, setTarget] = useState<Order | null>(null);

  const columns: Column<Order>[] = [
    { key: "created", header: "Data", cell: (o) => <span className="num">{formatDateTime(o.created_at)}</span> },
    {
      key: "symbol",
      header: "Simbolo",
      sortValue: (o) => o.symbol,
      cell: (o) => (
        <>
          {o.symbol}
          <LiveMarker state={liveState} />
        </>
      ),
    },
    { key: "side", header: "Lato", cell: (o) => (o.side === "buy" ? "Acquisto" : "Vendita") },
    {
      key: "type",
      header: "Tipo",
      cell: (o) => (
        <span className="flex flex-col items-start">
          {o.type}
          {real && o.type === "trailing_stop" && <span className="text-xs text-warn">{TRAILING_NOTE}</span>}
        </span>
      ),
    },
    {
      key: "qty",
      header: "Quantità",
      align: "right",
      cell: (o) => <span className="num">{o.qty ? formatQty(o.qty) : o.notional ? `${formatQty(o.notional)} (nozionale)` : "—"}</span>,
    },
    { key: "price", header: "Prezzo", align: "right", cell: (o) => <span className="num">{orderPrice(o)}</span> },
    { key: "filled", header: "Eseguita", align: "right", cell: (o) => <span className="num">{formatQty(o.filled_qty)}</span> },
    {
      key: "status",
      header: "Stato",
      cell: (o) => (
        <span title={o.reject_message ?? undefined}>
          <Badge variant={o.status === "rejected" ? "destructive" : "secondary"}>{ORDER_STATUS_LABELS[o.status] ?? o.status}</Badge>
        </span>
      ),
    },
    { key: "source", header: "Origine", cell: (o) => o.source },
    { key: "broker", header: "Broker", cell: (o) => <BrokerCell order={o} linked={linked} /> },
    {
      key: "actions",
      header: <span className="sr-only">Azioni</span>,
      align: "right",
      cell: (o) =>
        !readOnly && OPEN_ORDER_STATUSES.has(o.status) ? (
          <Button
            size="sm"
            variant="outline"
            onClick={() => {
              cancel.reset();
              setTarget(o);
            }}
          >
            Annulla
          </Button>
        ) : null,
    },
  ];

  return (
    <Card>
      <CardContent className="space-y-4 pt-6">
        <div className="flex items-center gap-2">
          <label htmlFor="order-status" className="text-sm text-muted">
            Stato
          </label>
          <select id="order-status" className={`${selectClass} w-48`} value={status} onChange={(e) => setStatus(e.target.value)}>
            {ORDER_FILTERS.map((f) => (
              <option key={f.value} value={f.value}>
                {f.label}
              </option>
            ))}
          </select>
          {!readOnly && (
            <Button className="ml-auto" onClick={() => setOrdering(true)}>
              <Plus className="h-4 w-4" /> Nuovo ordine
            </Button>
          )}
        </div>
        {isError && <p className="text-sm text-loss">{errorMessage(error, "Impossibile caricare gli ordini")}</p>}
        <DataTable aria-label="Ordini" columns={columns} rows={data} rowKey={(o) => o.id} loading={isLoading} empty="Nessun ordine" />
      </CardContent>
      {ordering && <OrderForm portfolio={portfolio} onClose={() => setOrdering(false)} />}
      {target && (
        <ConfirmDialog
          open
          onOpenChange={(o) => !o && setTarget(null)}
          title="Annullare l'ordine?"
          description={`${target.side === "buy" ? "Acquisto" : "Vendita"} ${target.symbol}`}
          confirmLabel="Annulla ordine"
          destructive
          pending={cancel.isPending}
          error={cancel.error ? errorMessage(cancel.error) : null}
          onConfirm={() =>
            cancel.mutate(target.id, {
              onSuccess: () => {
                toast.success("Ordine annullato");
                setTarget(null);
              },
            })
          }
        />
      )}
    </Card>
  );
}

// --- Rischio -------------------------------------------------------------------------

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex justify-between gap-4 py-1.5 text-sm">
      <dt className="text-muted">{label}</dt>
      <dd className="text-right">{children}</dd>
    </div>
  );
}

const MARGIN_LEVEL = {
  ok: { label: "Margine regolare", variant: "default" },
  warning: { label: "Sotto la soglia di avviso", variant: "warning" },
  maintenance: { label: "Sotto il mantenimento", variant: "destructive" },
} as const;

function Risk({ portfolio }: { portfolio: Portfolio }) {
  const valuation = useValuation(portfolio.id);
  const profiles = useRiskProfiles();
  const patch = usePatchPortfolio(portfolio.id);
  const [unlocking, setUnlocking] = useState(false);
  const profile = profiles.data?.find((p) => p.id === portfolio.risk_profile_id);
  const v: Valuation | undefined = valuation.data;
  const distance = v ? subtractDecimal(v.equity_eur, v.maint_req_eur) : null;
  // a real-money anomaly (reduce-only or locked, Ruling R8.13) is lifted only by «Ho verificato, riprendi» (TOTP)
  const blocked = (portfolio.status === "reduce_only" || portfolio.status === "locked") && !isLiveReason(portfolio.reduce_only_reason);

  return (
    <div className="grid gap-4 lg:grid-cols-2">
      <Card>
        <CardHeader className="flex flex-row items-center justify-between gap-2 space-y-0">
          <CardTitle>Stato</CardTitle>
          {blocked && (
            <Button
              size="sm"
              onClick={() => {
                patch.reset();
                setUnlocking(true);
              }}
            >
              Sblocca
            </Button>
          )}
        </CardHeader>
        <CardContent>
          <dl className="divide-y divide-border">
            <Row label="Stato">
              <PortfolioStatusBadge status={portfolio.status} />
            </Row>
            {portfolio.reduce_only_reason && <Row label="Motivo">{reduceOnlyReasonLabel(portfolio.reduce_only_reason)}</Row>}
            {portfolio.reduce_only_until && (
              <Row label="Fino a">
                <span className="num">{formatDateTime(portfolio.reduce_only_until)}</span>
              </Row>
            )}
            <Row label="Leva">
              <span className="num">{formatDecimal(portfolio.leverage, 2)}×</span>
            </Row>
            <Row label="Profilo di rischio">{profile?.name ?? (portfolio.risk_profile_id ? "—" : "Nessuno")}</Row>
            {profile && (
              <>
                <Row label="Max nozionale per ordine">
                  <Money value={profile.max_order_notional_eur} />
                </Row>
                <Row label="Max peso posizione">
                  <span className="num">{profile.max_position_pct ? `${formatDecimal(profile.max_position_pct, 2)}%` : "—"}</span>
                </Row>
                <Row label="Max perdita giornaliera">
                  <span className="num">
                    {profile.max_daily_loss_pct ? `${formatDecimal(profile.max_daily_loss_pct, 2)}%` : "—"}
                    {profile.max_daily_loss_eur ? ` · ${formatMoney(profile.max_daily_loss_eur, "EUR")}` : ""}
                  </span>
                </Row>
              </>
            )}
          </dl>
        </CardContent>
      </Card>
      <Card>
        <CardHeader>
          <CardTitle>Margine</CardTitle>
        </CardHeader>
        <CardContent>
          {valuation.isError ? (
            <p className="text-sm text-loss">{errorMessage(valuation.error, "Valutazione non disponibile")}</p>
          ) : !v ? (
            <p className="text-sm text-muted">Caricamento…</p>
          ) : (
            <dl className="divide-y divide-border">
              <Row label="Livello">
                <Badge variant={MARGIN_LEVEL[v.margin_level].variant}>{MARGIN_LEVEL[v.margin_level].label}</Badge>
              </Row>
              <Row label="Equity">
                <Money value={v.equity_eur} />
              </Row>
              <Row label="Esposizione">
                <Money value={v.exposure_eur} />
              </Row>
              <Row label="Margine iniziale richiesto">
                <Money value={v.initial_req_eur} />
              </Row>
              <Row label={`Soglia di avviso (${formatDecimal(portfolio.warning_margin_pct, 2)}%)`}>
                <Money value={v.warn_req_eur} />
              </Row>
              <Row label={`Mantenimento (${formatDecimal(portfolio.maintenance_margin_pct, 2)}%)`}>
                <Money value={v.maint_req_eur} />
              </Row>
              <Row label="Distanza dal mantenimento">
                <Pnl value={distance} />
              </Row>
            </dl>
          )}
        </CardContent>
      </Card>
      {unlocking && (
        <ConfirmDialog
          open
          onOpenChange={(o) => !o && setUnlocking(false)}
          title="Sbloccare il portafoglio?"
          description="Il portafoglio torna attivo: gli agenti potranno aprire nuove posizioni."
          confirmLabel="Sblocca"
          pending={patch.isPending}
          error={patch.error ? errorMessage(patch.error) : null}
          onConfirm={() =>
            patch.mutate(
              { status: "active" },
              {
                onSuccess: () => {
                  toast.success("Portafoglio sbloccato");
                  setUnlocking(false);
                },
              },
            )
          }
        />
      )}
    </div>
  );
}

// --- real money ----------------------------------------------------------------------

/** A real-money portfolio: «Ho verificato, riprendi» and «Chiudi tutte le posizioni reali» (TOTP). */
function LivePanel({ portfolio }: { portfolio: Portfolio }) {
  const resume = useLiveResume(portfolio.id);
  const flatten = useLiveFlatten(portfolio.id);
  const [dialog, setDialog] = useState<"resume" | "flatten" | null>(null);
  const state = portfolio.live_state!;
  const suspended = (portfolio.status === "reduce_only" || portfolio.status === "locked") && isLiveReason(portfolio.reduce_only_reason);
  const links = useLiveLinks();
  const link = links.data?.find((l) => l.id === portfolio.broker_link_id);
  return (
    <section
      aria-label="Denaro reale"
      className="mb-4 flex flex-wrap items-center justify-between gap-3 rounded-md border-2 border-loss bg-loss/10 px-4 py-3 text-sm"
    >
      <div className="space-y-0.5">
        <p className="flex items-center gap-2 font-semibold text-loss">
          <ShieldAlert className="h-4 w-4" aria-hidden />
          Portafoglio con denaro reale · {LIVE_STATE_LABELS[state] ?? state}
        </p>
        {suspended && (
          <p>
            {portfolio.status === "locked" ? "Bloccato" : "In reduce-only"}: {reduceOnlyReasonLabel(portfolio.reduce_only_reason!)}. Verifica il conto al broker prima di riprendere.
          </p>
        )}
      </div>
      <div className="flex flex-wrap items-start gap-2">
        {portfolio.broker_link_id && (
          <LiveDisarmButton
            linkId={portfolio.broker_link_id}
            linkName={link?.name ?? "il collegamento reale"}
            liveState={link?.live_state ?? state}
            everArmed={Boolean(link?.armed_at || link?.disarmed_at)}
          />
        )}
        {suspended && (
          <Button
            size="sm"
            onClick={() => {
              resume.reset();
              setDialog("resume");
            }}
          >
            Ho verificato, riprendi
          </Button>
        )}
        {state === "armed" && (
          <Button
            size="sm"
            variant="destructive"
            onClick={() => {
              flatten.reset();
              setDialog("flatten");
            }}
          >
            Chiudi tutte le posizioni reali
          </Button>
        )}
      </div>
      {dialog === "resume" && (
        <LiveTotpDialog
          title="Riprendere il portafoglio reale?"
          description="Il portafoglio torna attivo: gli agenti potranno di nuovo aprire posizioni con denaro reale."
          confirmLabel="Riprendi"
          pending={resume.isPending}
          error={resume.error}
          onClose={() => setDialog(null)}
          onConfirm={(totp) =>
            resume.mutate(totp, {
              onSuccess: () => {
                toast.success("Portafoglio ripreso");
                setDialog(null);
              },
            })
          }
        />
      )}
      {dialog === "flatten" && (
        <LiveTotpDialog
          title="Chiudere tutte le posizioni reali?"
          description="Un ordine a mercato reduce-only per ogni posizione aperta, inviato al broker anche con il kill switch acceso."
          confirmLabel="Chiudi tutte le posizioni"
          destructive
          pending={flatten.isPending}
          error={flatten.error}
          onClose={() => setDialog(null)}
          onConfirm={(totp) =>
            flatten.mutate(totp, {
              onSuccess: (res) => {
                toast.success(`Ordini di chiusura inviati: ${res.orders.length}`);
                setDialog(null);
              },
            })
          }
        />
      )}
    </section>
  );
}

// --- archived banner -----------------------------------------------------------------

function ArchivedBanner({ portfolio }: { portfolio: Portfolio }) {
  const restore = useArchivePortfolio();
  const [confirming, setConfirming] = useState(false);
  return (
    <div
      role="status"
      aria-label="Portafoglio archiviato"
      className="mb-4 flex flex-wrap items-center justify-between gap-3 rounded-md border border-border bg-card px-4 py-3 text-sm"
    >
      <p>
        <strong>Portafoglio archiviato</strong>
        {portfolio.archived_at && <> il {formatDateTime(portfolio.archived_at)}</>}
        {portfolio.archived_by && <> da {portfolio.archived_by}</>}: sola lettura, escluso da dashboard, agenti e job.
      </p>
      <Button
        size="sm"
        variant="outline"
        onClick={() => {
          restore.reset();
          setConfirming(true);
        }}
      >
        <ArchiveRestore />
        Ripristina
      </Button>
      {confirming && (
        <ConfirmDialog
          open
          onOpenChange={(o) => !o && setConfirming(false)}
          title={`Ripristinare ${portfolio.name}?`}
          description="Il portafoglio torna visibile con lo stato che aveva prima dell'archiviazione."
          confirmLabel="Ripristina"
          pending={restore.isPending}
          error={restore.error ? errorMessage(restore.error) : null}
          onConfirm={() =>
            restore.mutate(
              { id: portfolio.id, archive: false },
              {
                onSuccess: () => {
                  toast.success("Portafoglio ripristinato");
                  setConfirming(false);
                },
              },
            )
          }
        />
      )}
    </div>
  );
}

// --- page ----------------------------------------------------------------------------

export default function PortfolioDetail() {
  const { id = "" } = useParams();
  const { data: portfolio, isError, error } = usePortfolio(id);
  const archived = portfolio?.status === "archived";

  return (
    <div>
      <Link to="/portafogli" className="mb-2 inline-flex items-center gap-1 text-sm text-muted hover:text-fg">
        <ArrowLeft className="h-4 w-4" /> Portafogli
      </Link>
      <PageHeader title={portfolio?.name ?? "Portafoglio"}>
        {portfolio && (
          <div className="flex items-center gap-2">
            <PortfolioModeBadge mode={portfolio.mode} liveState={portfolio.live_state} />
            <PortfolioStatusBadge status={portfolio.status} />
          </div>
        )}
      </PageHeader>
      {isError && <p className="text-loss">{errorMessage(error, "Impossibile caricare il portafoglio")}</p>}
      {portfolio && archived && <ArchivedBanner portfolio={portfolio} />}
      {portfolio && !archived && <HaltBanner portfolioId={portfolio.id} />}
      {portfolio && !archived && portfolio.live_state && <LivePanel portfolio={portfolio} />}
      {!portfolio ? (
        !isError && <p className="text-sm text-muted">Caricamento…</p>
      ) : (
        <Tabs defaultValue="overview">
          <TabsList className="mb-4 flex-wrap">
            <TabsTrigger value="overview">Panoramica</TabsTrigger>
            <TabsTrigger value="positions">Posizioni</TabsTrigger>
            <TabsTrigger value="orders">Ordini</TabsTrigger>
            <TabsTrigger value="movements">Movimenti</TabsTrigger>
            {!archived && <TabsTrigger value="operations">Operazioni</TabsTrigger>}
            <TabsTrigger value="risk">Rischio</TabsTrigger>
            {!archived && <TabsTrigger value="settings">Impostazioni</TabsTrigger>}
          </TabsList>
          <TabsContent value="overview">
            <Overview portfolio={portfolio} />
          </TabsContent>
          <TabsContent value="positions">
            <Positions portfolioId={portfolio.id} liveState={portfolio.live_state} />
          </TabsContent>
          <TabsContent value="orders">
            <Orders portfolio={portfolio} readOnly={archived} />
          </TabsContent>
          <TabsContent value="movements">
            <Movements portfolioId={portfolio.id} timezone={portfolio.timezone} readOnly={archived} />
          </TabsContent>
          {!archived && (
            <TabsContent value="operations">
              <Operations portfolio={portfolio} />
            </TabsContent>
          )}
          <TabsContent value="risk">
            <Risk portfolio={portfolio} />
          </TabsContent>
          {!archived && (
            <TabsContent value="settings">
              <PortfolioSettings portfolio={portfolio} />
            </TabsContent>
          )}
        </Tabs>
      )}
    </div>
  );
}
