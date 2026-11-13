import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { useDashboard, useLiveFeed, FEED_EVENT_LABELS } from "@/api/hooks/dashboard";
import type { Dashboard as DashboardData, DashboardConnector, MarginAlert } from "@/api/types";
import { EquityChart } from "@/components/EquityChart";
import { KillSwitchButton } from "@/components/KillSwitchButton";
import { Money } from "@/components/Money";
import { PageHeader } from "@/components/PageHeader";
import { Pnl } from "@/components/Pnl";
import { PortfolioModeBadge } from "@/components/PortfolioModeBadge";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { formatDateTime, formatMoney, formatQty } from "@/lib/format";
import { MODE_INFO } from "@/lib/portfolioMode";
import { heartbeatState } from "@/lib/heartbeat";
import { cn } from "@/lib/utils";

const TONE_DOT = { ok: "bg-gain", warn: "bg-warn", down: "bg-loss" } as const;
const CONNECTOR_TONE = { ok: "ok", error: "down" } as const;

function Dot({ tone, label }: { tone: "ok" | "warn" | "down" | "off"; label: string }) {
  return (
    <span
      role="img"
      aria-label={label}
      title={label}
      className={cn("inline-block h-2.5 w-2.5 rounded-full", tone === "off" ? "bg-muted" : TONE_DOT[tone])}
    />
  );
}

function Kpi({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-sm font-medium text-muted">{title}</CardTitle>
      </CardHeader>
      <CardContent className="text-2xl font-semibold">{children}</CardContent>
    </Card>
  );
}

function ConnectorStatus({ connectors }: { connectors: DashboardConnector[] }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Connettori</CardTitle>
      </CardHeader>
      <CardContent>
        {connectors.length === 0 ? (
          <p className="text-sm text-muted">Nessun connettore</p>
        ) : (
          <ul className="space-y-2 text-sm">
            {connectors.map((c) => {
              const tone = !c.enabled ? "off" : (CONNECTOR_TONE[c.health_status as "ok" | "error"] ?? "warn");
              const state = !c.enabled ? "disabilitato" : c.health_status === "ok" ? "operativo" : c.health_status === "error" ? "errore" : "sconosciuto";
              return (
                <li key={c.name} className="flex items-center gap-2">
                  <Dot tone={tone} label={`${c.name}: ${state}`} />
                  <span>{c.name}</span>
                  <span className="text-muted">{state}</span>
                </li>
              );
            })}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}

function alertText(a: MarginAlert): string {
  return a.kind === "liquidation" ? "Liquidazione" : "Avviso di margine";
}

function MarginAlerts({ alerts, names }: { alerts: MarginAlert[]; names: Map<string, string> }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Alert di margine</CardTitle>
      </CardHeader>
      <CardContent>
        {alerts.length === 0 ? (
          <p className="text-sm text-muted">Nessun alert</p>
        ) : (
          <ul className="space-y-2 text-sm">
            {alerts.map((a) => (
              <li key={a.id} className="flex items-center justify-between gap-2">
                <span className={a.kind === "liquidation" ? "text-loss" : "text-warn"}>
                  {alertText(a)}
                  {a.portfolio_id && names.has(a.portfolio_id) ? ` · ${names.get(a.portfolio_id)}` : ""}
                </span>
                <span className="text-muted">{formatDateTime(a.created_at)}</span>
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}

/** «Live» while the WS is connected (green dot when the data is < 10 s old), «In pausa»
 * only when it is disconnected; always with the time of the last update. */
export const LIVE_FRESH_MS = 10_000;

function LiveIndicator({ live, updatedAt }: { live: boolean; updatedAt: number }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, []);
  const fresh = live && updatedAt > 0 && now - updatedAt < LIVE_FRESH_MS;
  const time = updatedAt ? new Date(updatedAt).toLocaleTimeString("it-IT") : "—";
  return (
    <span role="status" aria-label="Aggiornamento dashboard" className="inline-flex items-center gap-2 text-xs text-muted">
      <span className={cn("inline-block h-2 w-2 rounded-full", fresh ? "bg-gain" : live ? "bg-gain/50" : "bg-muted")} aria-hidden />
      <span className={live ? "font-medium text-gain" : ""}>{live ? "Live" : "In pausa"}</span>
      <span>{` · ultimo aggiornamento ${time}`}</span>
    </span>
  );
}

function str(v: unknown): string {
  return typeof v === "string" ? v : "";
}

function LiveFeed() {
  const events = useLiveFeed();
  return (
    <Card>
      <CardHeader>
        <CardTitle>Ordini ed eseguiti in tempo reale</CardTitle>
      </CardHeader>
      <CardContent>
        {events.length === 0 ? (
          <p className="text-sm text-muted">In attesa di eventi…</p>
        ) : (
          <ul className="max-h-72 space-y-1 overflow-auto text-sm" aria-label="Feed ordini">
            {events.map((e, i) => (
              <li key={`${e.ts ?? ""}-${i}`} className="flex flex-wrap items-center gap-2">
                <span className="text-muted">{e.ts ? formatDateTime(e.ts) : ""}</span>
                <Badge variant={e.type === "order.filled" ? "default" : e.type === "order.rejected" ? "destructive" : "secondary"}>
                  {FEED_EVENT_LABELS[e.type]}
                </Badge>
                <span className="num">
                  {str(e.symbol)}
                  {e.type === "order.filled" && (
                    <>
                      {" "}
                      {str(e.side) === "buy" ? "acquisto" : str(e.side) === "sell" ? "vendita" : str(e.side)}{" "}
                      {formatQty(str(e.qty))} @ {formatMoney(str(e.price), str(e.currency) || "EUR")}
                    </>
                  )}
                </span>
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}

function ModeCounts({ modes }: { modes: DashboardData["modes"] }) {
  return (
    <ul aria-label="Portafogli per modalità" className="mt-1 flex flex-wrap gap-x-3 text-xs font-normal text-muted">
      {(["simulazione", "paper", "reale"] as const)
        .filter((m) => m !== "reale" || modes[m] > 0)
        .map((m) => (
          <li key={m}>
            {MODE_INFO[m].icon} {MODE_INFO[m].label.toLowerCase()}: <span className="num">{modes[m] ?? 0}</span>
          </li>
        ))}
    </ul>
  );
}

function PortfolioTable({ portfolios }: { portfolios: DashboardData["portfolios"] }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Portafogli</CardTitle>
      </CardHeader>
      <CardContent>
        {portfolios.length === 0 ? (
          <p className="text-sm text-muted">Nessun portafoglio</p>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Nome</TableHead>
                <TableHead className="text-right">Equity</TableHead>
                <TableHead className="text-right">P&amp;L del giorno</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {portfolios.map((p) => (
                <TableRow key={p.id}>
                  <TableCell>
                    <Link to={`/portafogli/${p.id}`} className="text-accent hover:underline">
                      {p.name}
                    </Link>
                    <PortfolioModeBadge mode={p.mode} className="ml-2" />
                    {p.error && <span className="ml-2 text-xs text-loss">valutazione non disponibile</span>}
                    {p.stale_symbols.length > 0 && <span className="ml-2 text-xs text-warn">quotazioni non aggiornate</span>}
                  </TableCell>
                  <TableCell className="text-right">
                    <Money value={p.equity_eur} />
                  </TableCell>
                  <TableCell className="text-right">
                    <Pnl value={p.day_pnl_eur} pct={p.day_pnl_pct} />
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </CardContent>
    </Card>
  );
}

export default function Dashboard() {
  const { data, isError, live, dataUpdatedAt } = useDashboard();

  return (
    <div>
      <PageHeader title="Dashboard">
        <LiveIndicator live={live} updatedAt={dataUpdatedAt} />
        <KillSwitchButton />
      </PageHeader>
      {isError && <p className="text-loss">Impossibile caricare la dashboard</p>}
      {!data ? (
        !isError && <p className="text-sm text-muted">Caricamento…</p>
      ) : (
        <div className="space-y-6">
          <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
            <Kpi title="Equity totale">
              <Money value={data.total_equity_eur} />
            </Kpi>
            <Kpi title="P&L del giorno">
              <Pnl value={data.day_pnl_eur} pct={data.day_pnl_pct} />
            </Kpi>
            <Kpi title="Portafogli">
              <span className="num">{data.portfolios.length}</span>
              <ModeCounts modes={data.modes} />
            </Kpi>
            <Kpi title="Worker">
              {(() => {
                const hb = heartbeatState(data.worker_heartbeat_age_s);
                return (
                  <span className="flex items-center gap-2 text-base">
                    <Dot tone={hb.tone} label={hb.label} />
                    {hb.label}
                  </span>
                );
              })()}
            </Kpi>
          </div>
          <Card>
            <CardHeader>
              <CardTitle>Equity aggregata</CardTitle>
            </CardHeader>
            <CardContent>
              <EquityChart live={dataUpdatedAt ? { ts: dataUpdatedAt, equity_eur: data.total_equity_eur } : undefined} />
            </CardContent>
          </Card>
          <PortfolioTable portfolios={data.portfolios} />
          <div className="grid gap-4 lg:grid-cols-2">
            <LiveFeed />
            <div className="space-y-4">
              <ConnectorStatus connectors={data.connectors} />
              <MarginAlerts alerts={data.margin_alerts} names={new Map(data.portfolios.map((p) => [p.id, p.name]))} />
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
