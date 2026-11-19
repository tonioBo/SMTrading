import { Link } from "react-router-dom";
import { useOpenPositions } from "@/api/hooks/positions";
import type { OpenPosition, OpenPositionsPortfolio, ProtectiveLeg } from "@/api/types";
import { DataTable, type Column } from "@/components/DataTable";
import { ExtendedPositionLine, GapRiskBadge } from "@/components/ExtendedHours";
import { Money } from "@/components/Money";
import { PageHeader } from "@/components/PageHeader";
import { Pnl } from "@/components/Pnl";
import { PortfolioModeBadge } from "@/components/PortfolioModeBadge";
import { ProtectionBadge } from "@/components/ProtectionBadge";
import { StatusBadge } from "@/components/StatusBadge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { formatDateTime, formatDecimal, formatMoney, formatQty } from "@/lib/format";
import { cn } from "@/lib/utils";

const TIF_LABEL: Record<ProtectiveLeg["tif"], string> = { day: "giornaliero", gtc: "fino a revoca" };

function price(v: string | null | undefined, currency: string): string {
  return v == null || v === "" ? "—" : formatMoney(v, currency);
}

function Leg({ leg, currency }: { leg: ProtectiveLeg | null; currency: string }) {
  if (!leg) return <span className="text-muted">—</span>;
  return (
    <span className="whitespace-nowrap">
      <span className="num">{price(leg.price, currency)}</span>
      <span className={cn("ml-1 text-xs", leg.tif === "day" ? "text-warn" : "text-muted")}>{TIF_LABEL[leg.tif]}</span>
    </span>
  );
}

/** «Rischio agli stop: 7,75 € su 40,00 € (19%)» */
function riskLine(t: OpenPositionsPortfolio): string {
  const risk = formatMoney(t.risk_at_stop_eur, "EUR");
  if (t.daily_loss_limit_eur == null) return `Rischio agli stop: ${risk} (nessun limite di perdita giornaliera)`;
  const pct = t.risk_pct_of_limit == null ? "" : ` (${formatDecimal(t.risk_pct_of_limit, 0)}%)`;
  return `Rischio agli stop: ${risk} su ${formatMoney(t.daily_loss_limit_eur, "EUR")}${pct}`;
}

/** A position being closed at the end of a «piano»: «In chiusura piano fino al …» in grace. */
function PlanBadge({ p }: { p: OpenPosition }) {
  if (!p.plan_state) return null;
  const text = p.plan_grace_until ? `In chiusura piano fino al ${formatDateTime(p.plan_grace_until)}` : "In chiusura piano";
  return (
    <StatusBadge tone="warn" title="fine periodo di un piano: nessuna aggiunta, poi chiusura">
      {text}
    </StatusBadge>
  );
}

const positionColumns: Column<OpenPosition>[] = [
  {
    key: "symbol",
    header: "Simbolo",
    primary: true,
    cell: (p) => (
      <div>
        <div className="font-medium">{p.symbol}</div>
        {p.name && <div className="text-xs font-normal text-muted">{p.name}</div>}
      </div>
    ),
  },
  { key: "qty", header: "Quantità", align: "right", className: "num", cell: (p) => `${p.side === "short" ? "short " : ""}${formatQty(p.qty)}` },
  { key: "avg", header: "Prezzo medio", align: "right", className: "num", cell: (p) => price(p.avg_price, p.currency) },
  {
    key: "last",
    header: "Ultimo",
    align: "right",
    className: "num",
    cell: (p) => (
      <>
        {price(p.last, p.currency)}
        {p.stale && (
          <span className="ml-1 text-xs text-warn" title="ultima quotazione in memoria">
            *
          </span>
        )}
        <ExtendedPositionLine ext={p.extended} currency={p.currency} />
      </>
    ),
  },
  { key: "value", header: "Valore", align: "right", cell: (p) => <Money value={p.value_eur} /> },
  { key: "pnl", header: "P/L", align: "right", cell: (p) => <Pnl value={p.pnl_eur} pct={p.pnl_pct} /> },
  { key: "stop", header: "Stop", cell: (p) => <Leg leg={p.stop} currency={p.currency} /> },
  { key: "target", header: "Target", cell: (p) => <Leg leg={p.target} currency={p.currency} /> },
  {
    key: "distance",
    header: "Distanza dallo stop",
    align: "right",
    className: "num",
    cell: (p) => (p.distance_to_stop_pct == null ? "—" : `${formatDecimal(p.distance_to_stop_pct, 2)}%`),
  },
  { key: "risk", header: "Rischio allo stop", align: "right", cell: (p) => <Money value={p.risk_at_stop_eur} /> },
  {
    key: "protection",
    header: "Protezione",
    cell: (p) => (
      <div className="flex flex-wrap justify-end gap-1 sm:justify-start">
        <ProtectionBadge protection={p.protection} />
        {p.extended_beyond_stop && <GapRiskBadge side={p.side} />}
        <PlanBadge p={p} />
      </div>
    ),
  },
];

function PortfolioCard({ t, rows }: { t: OpenPositionsPortfolio; rows: OpenPosition[] }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex flex-wrap items-center gap-2">
          <Link to={`/portafogli/${t.portfolio_id}`} className="text-accent hover:underline">
            {t.portfolio_name}
          </Link>
          <PortfolioModeBadge mode={t.mode} liveState={t.live_state} label={t.mode_label} />
        </CardTitle>
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-sm">
          <span className="num" aria-label={`Rischio ${t.portfolio_name}`}>
            {riskLine(t)}
          </span>
          {t.unprotected > 0 && <span className="text-loss">{`${t.unprotected} senza stop`}</span>}
          {t.partial_cover > 0 && <span className="text-warn">{`${t.partial_cover} copre parziale`}</span>}
          {t.risk_unknown > 0 && <span className="text-muted">{`${t.risk_unknown} senza quotazione: rischio non calcolato`}</span>}
          <span className="text-muted">
            Valore <Money value={t.value_eur} /> · P/L <Pnl value={t.pnl_eur} />
          </span>
        </div>
      </CardHeader>
      <CardContent>
        <DataTable aria-label={`Posizioni ${t.portfolio_name}`} columns={positionColumns} rows={rows} rowKey={(p) => p.symbol} empty="Nessuna posizione" />
      </CardContent>
    </Card>
  );
}

export default function Positions() {
  const { data, isError, isLoading } = useOpenPositions();
  const totals = data?.portfolios ?? [];
  return (
    <div>
      <PageHeader title="Posizioni">
        {data?.as_of && <span className="text-xs text-muted">{`ultimo aggiornamento ${formatDateTime(data.as_of)}`}</span>}
      </PageHeader>
      {isError && <p className="text-loss">Impossibile caricare le posizioni</p>}
      {isLoading ? (
        <p className="text-sm text-muted">Caricamento…</p>
      ) : (
        data &&
        (totals.length === 0 ? (
          <p className="text-sm text-muted">Nessuna posizione aperta</p>
        ) : (
          <div className="space-y-6">
            {totals.map((t) => (
              <PortfolioCard key={t.portfolio_id} t={t} rows={data.positions.filter((p) => p.portfolio_id === t.portfolio_id)} />
            ))}
          </div>
        ))
      )}
    </div>
  );
}
