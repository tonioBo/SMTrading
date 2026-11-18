import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { useDashboard, useLiveFeed, FEED_EVENT_LABELS } from "@/api/hooks/dashboard";
import { useGoalsProgress } from "@/api/hooks/goals";
import { useOpenPositions } from "@/api/hooks/positions";
import { useSignals } from "@/api/hooks/signals";
import type { Dashboard as DashboardData, DashboardConnector, GoalProgress, MarginAlert } from "@/api/types";
import { EquityHistoryCard } from "@/components/EquityHistoryCard";
import { GoalBar } from "@/components/GoalBar";
import { GoalParts } from "@/components/GoalParts";
import { HaltBanner } from "@/components/HaltBanner";
import { KillSwitchButton } from "@/components/KillSwitchButton";
import { Money } from "@/components/Money";
import { PageHeader } from "@/components/PageHeader";
import { Pct } from "@/components/Pct";
import { Pnl } from "@/components/Pnl";
import { GapRiskBadge } from "@/components/ExtendedHours";
import { ProtectionBadge } from "@/components/ProtectionBadge";
import { SignalBadge } from "@/components/SignalBadges";
import { StatusBadge } from "@/components/StatusBadge";
import { PortfolioModeBadge } from "@/components/PortfolioModeBadge";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { formatDateTime, formatMoney, formatQty } from "@/lib/format";
import { comparisonText, formatDay, goalScopeLabel, goalTargetLabel, lossText, STATUS_TONE } from "@/lib/goals";
import { closestSignals } from "@/lib/signals";
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

function shortDay(day: string): string {
  return formatDay(day).slice(0, 5);
}

/** Under a goal: the bar (minimum/cap), the loss against the limit (risk) or the index's
 * % and the difference (index); nothing before the period has data. */
function GoalsCardFigures({ g }: { g: GoalProgress }) {
  if (g.pct == null) return null;
  if (g.kind === "beat_index") return <p className="text-xs text-muted">{comparisonText(g)}</p>;
  if (g.kind === "max_loss") {
    const loss = lossText(g);
    return (
      <>
        <GoalBar pct={g.loss_pct ?? null} target={g.target_pct} kind="max_loss" label={`Perdita ${g.name}`} />
        {loss && <p className="text-xs text-muted">{loss}</p>}
      </>
    );
  }
  return <GoalBar pct={g.pct} target={g.target_pct} kind={g.kind} elapsed={g.elapsed_pct} label={`Avanzamento ${g.name}`} />;
}

/** «Obiettivi»: every active goal's current period, compact (the page has the details);
 * hidden without active goals. */
function GoalsCard({ goals }: { goals: GoalProgress[] }) {
  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between gap-2 space-y-0 pb-2">
        <CardTitle>Obiettivi</CardTitle>
        <Link to="/obiettivi" className="text-sm text-accent hover:underline" aria-label="Gestisci obiettivi">
          Gestisci
        </Link>
      </CardHeader>
      <CardContent>
        <ul className="grid gap-x-6 gap-y-3 md:grid-cols-2" aria-label="Obiettivi attivi">
          {goals.map((g) => (
            <li key={g.goal_id} className="space-y-1 text-sm">
              <div className="flex flex-wrap items-baseline justify-between gap-2">
                <span className="flex flex-wrap items-baseline gap-2">
                  <span className="font-medium">{g.name}</span>
                  <span className="num text-muted">{goalTargetLabel(g.kind, g.target_pct, g.benchmark)}</span>
                  <span className="text-xs text-muted">{`${goalScopeLabel(g)} · ${shortDay(g.period_start)} – ${shortDay(g.period_end)}`}</span>
                </span>
                <span className="flex items-center gap-2">
                  {g.pct != null && <Pct value={g.pct} tone />}
                  <StatusBadge tone={STATUS_TONE[g.status] ?? "neutral"}>{g.status}</StatusBadge>
                </span>
              </div>
              <GoalsCardFigures g={g} />
              <GoalParts goal={g} />
            </li>
          ))}
        </ul>
      </CardContent>
    </Card>
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

const COMPACT_ROWS = 5;

/** «Posizioni aperte» (compact): symbol, P/L, protection; the full page is «Posizioni». */
function OpenPositionsCard() {
  const { data, isError } = useOpenPositions();
  const rows = data?.positions ?? [];
  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
        <CardTitle>Posizioni aperte</CardTitle>
        <Link to="/posizioni" className="text-sm text-accent hover:underline">
          Tutte le posizioni
        </Link>
      </CardHeader>
      <CardContent>
        {isError ? (
          <p className="text-sm text-loss">Impossibile caricare le posizioni</p>
        ) : !data ? (
          <p className="text-sm text-muted">Caricamento…</p>
        ) : rows.length === 0 ? (
          <p className="text-sm text-muted">Nessuna posizione aperta</p>
        ) : (
          <ul className="divide-y divide-border text-sm" aria-label="Posizioni aperte">
            {rows.slice(0, COMPACT_ROWS).map((p) => (
              <li key={`${p.portfolio_id}:${p.symbol}`} className="flex flex-wrap items-center justify-between gap-2 py-2">
                <span className="min-w-0">
                  <span className="font-medium">{p.symbol}</span>
                  <Link to={`/portafogli/${p.portfolio_id}`} className="ml-2 text-xs text-muted hover:underline">
                    {p.portfolio_name}
                  </Link>
                </span>
                <span className="flex items-center gap-2">
                  <Pnl value={p.pnl_eur} pct={p.pnl_pct} />
                  <ProtectionBadge protection={p.protection} />
                  {p.extended_beyond_stop && <GapRiskBadge side={p.side} />}
                </span>
              </li>
            ))}
          </ul>
        )}
        {rows.length > COMPACT_ROWS && <p className="mt-2 text-xs text-muted">{`e altre ${rows.length - COMPACT_ROWS}`}</p>}
      </CardContent>
    </Card>
  );
}

/** «Segnali più vicini»: the 5 symbols closest to an entry; the full page is «Segnali». */
function ClosestSignalsCard() {
  const { data, isError } = useSignals();
  const top = closestSignals(data, COMPACT_ROWS);
  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
        <CardTitle>Segnali più vicini</CardTitle>
        <Link to="/segnali" className="text-sm text-accent hover:underline">
          Tutti i segnali
        </Link>
      </CardHeader>
      <CardContent>
        {isError ? (
          <p className="text-sm text-loss">Impossibile caricare i segnali</p>
        ) : !data ? (
          <p className="text-sm text-muted">Caricamento…</p>
        ) : top.length === 0 ? (
          <p className="text-sm text-muted">Nessun segnale vicino</p>
        ) : (
          <ul className="divide-y divide-border text-sm" aria-label="Segnali più vicini">
            {top.map((t) => (
              <li key={`${t.portfolioId ?? "none"}:${t.ref}:${t.symbol.symbol}`} className="flex flex-wrap items-center justify-between gap-2 py-2">
                <span className="min-w-0">
                  <span className="font-medium">{t.symbol.symbol}</span>
                  <span className="ml-2 text-xs text-muted">{`${t.ref}${t.portfolioName ? ` · ${t.portfolioName}` : ""}`}</span>
                  <span className="num ml-2 text-xs text-muted">{t.symbol.indicator ?? ""}</span>
                </span>
                <SignalBadge s={t.symbol} type={t.type} />
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}

/** «2 simulati · 1 Alpaca prova · 0 reali»: one entry per mode_label, «0 reali» without real money. */
function ModeCounts({ counts }: { counts: DashboardData["mode_counts"] }) {
  const entries = counts.map(({ label, count, live }) => ({
    key: label,
    count,
    text: label === MODE_INFO.simulazione.label ? (count === 1 ? "simulato" : "simulati") : label.replaceAll(" · ", " "),
    live,
  }));
  if (!entries.some((e) => e.live)) entries.push({ key: "reali", count: 0, text: "reali", live: true });
  return (
    <ul aria-label="Portafogli per modalità" className="mt-1 flex flex-wrap gap-x-1 text-xs font-normal text-muted">
      {entries.map((e, i) => (
        <li key={e.key}>
          {i > 0 && " · "}
          <span className="num">{e.count}</span> {e.text}
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
                    <PortfolioModeBadge mode={p.mode} liveState={p.live_state} label={p.mode_label} className="ml-2" />
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
  const goals = useGoalsProgress().data;

  return (
    <div>
      <PageHeader title="Dashboard">
        <LiveIndicator live={live} updatedAt={dataUpdatedAt} />
        <KillSwitchButton />
      </PageHeader>
      <HaltBanner />
      {isError && <p className="text-loss">Impossibile caricare la dashboard</p>}
      {!data ? (
        !isError && <p className="text-sm text-muted">Caricamento…</p>
      ) : (
        <div className="space-y-6">
          {goals && goals.length > 0 && <GoalsCard goals={goals} />}
          <EquityHistoryCard />
          <div className="grid gap-4 lg:grid-cols-2">
            <OpenPositionsCard />
            <ClosestSignalsCard />
          </div>
          <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
            <Kpi title="Equity totale">
              <Money value={data.total_equity_eur} />
            </Kpi>
            <Kpi title="P&L del giorno">
              <Pnl value={data.day_pnl_eur} pct={data.day_pnl_pct} />
            </Kpi>
            <Kpi title="Portafogli">
              <span className="num">{data.portfolios.length}</span>
              <ModeCounts counts={data.mode_counts} />
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
