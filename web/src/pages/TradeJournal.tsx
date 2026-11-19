import { Link, useSearchParams } from "react-router-dom";
import { TRADES_PAGE_SIZE, useTrades } from "@/api/hooks/trades";
import type { TradeFilters, TradeOutcome, TradePeriod, TradeRow } from "@/api/types";
import { selectClass } from "@/components/FormField";
import { Money } from "@/components/Money";
import { PageHeader } from "@/components/PageHeader";
import { Pnl } from "@/components/Pnl";
import { OutcomeBadge } from "@/components/OutcomeBadge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Label } from "@/components/ui/label";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { errorMessage } from "@/lib/errors";
import { formatDateTime, formatDecimal, formatHolding, formatMoney, formatQty } from "@/lib/format";
import { NO_STRATEGY, OUTCOME_LABELS, PERIOD_LABELS } from "@/lib/trades";

const PERIODS = Object.keys(PERIOD_LABELS) as TradePeriod[];

function filtersFrom(sp: URLSearchParams): TradeFilters {
  const period = sp.get("period") as TradePeriod | null;
  const outcome = sp.get("outcome") as TradeOutcome | null;
  const page = Number(sp.get("page") ?? "1");
  return {
    period: period && PERIODS.includes(period) ? period : "all",
    portfolio_id: sp.get("portfolio_id") ?? "",
    strategy: sp.get("strategy") ?? "",
    symbol: sp.get("symbol") ?? "",
    outcome: outcome && outcome in OUTCOME_LABELS ? outcome : "",
    page: Number.isInteger(page) && page > 0 ? page : 1,
  };
}

function price(v: string | null, currency: string): string {
  return v == null ? "—" : formatMoney(v, currency);
}

function Strategy({ t }: { t: TradeRow }) {
  return (
    <div>
      <div className="num font-medium">{t.strategy_ref ?? NO_STRATEGY}</div>
      {t.proposal_ref && <div className="num text-xs text-muted">{t.proposal_ref}</div>}
    </div>
  );
}

function Exit({ t }: { t: TradeRow }) {
  if (!t.exit_reason_label) return <span className="text-muted">—</span>;
  const more = t.exit_reasons.length > 1 ? ` (${t.exit_reasons.length} uscite)` : "";
  return <span className="whitespace-nowrap">{`${t.exit_reason_label}${more}`}</span>;
}

function detailLink(t: TradeRow): string {
  return `/diario-operazioni/${encodeURIComponent(t.key)}`;
}

function TradesTable({ rows }: { rows: TradeRow[] }) {
  return (
    <Table aria-label="Operazioni">
      <TableHeader>
        <TableRow>
          <TableHead>Aperta</TableHead>
          <TableHead>Simbolo</TableHead>
          <TableHead>Portafoglio</TableHead>
          <TableHead>Strategia</TableHead>
          <TableHead className="text-right">Quantità</TableHead>
          <TableHead className="text-right">Ingresso</TableHead>
          <TableHead className="text-right">Uscita / attuale</TableHead>
          <TableHead>Motivo d'uscita</TableHead>
          <TableHead className="text-right">Risultato</TableHead>
          <TableHead className="text-right">Costi</TableHead>
          <TableHead className="text-right">Durata</TableHead>
          <TableHead>Esito</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {rows.map((t) => (
          <TableRow key={t.key}>
            <TableCell className="whitespace-nowrap">
              <Link to={detailLink(t)} className="text-accent hover:underline" aria-label={`Dettaglio ${t.symbol} ${formatDateTime(t.opened_at)}`}>
                {formatDateTime(t.opened_at)}
              </Link>
            </TableCell>
            <TableCell className="font-medium">
              {t.symbol}
              {t.direction === "short" && <span className="ml-1 text-xs text-muted">ribasso</span>}
            </TableCell>
            <TableCell>{t.portfolio_name}</TableCell>
            <TableCell>
              <Strategy t={t} />
            </TableCell>
            <TableCell className="num text-right">
              {formatQty(t.qty)}
              {t.outcome === "open" && t.open_qty !== t.qty && <span className="ml-1 text-xs text-muted">{`(aperte ${formatQty(t.open_qty)})`}</span>}
            </TableCell>
            <TableCell className="num text-right">{price(t.entry_price, t.currency)}</TableCell>
            <TableCell className="num text-right">{t.closed_at ? price(t.exit_price, t.currency) : price(t.mark, t.currency)}</TableCell>
            <TableCell>
              <Exit t={t} />
            </TableCell>
            <TableCell className="text-right">
              <Pnl value={t.pnl_eur} pct={t.pnl_pct} />
            </TableCell>
            <TableCell className="text-right">
              <Money value={t.costs_eur} />
            </TableCell>
            <TableCell className="num whitespace-nowrap text-right">{formatHolding(t.holding_s)}</TableCell>
            <TableCell>
              <OutcomeBadge outcome={t.outcome} />
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}

/** Small screens: one card per trade. */
function TradeCards({ rows }: { rows: TradeRow[] }) {
  return (
    <ul className="space-y-3" aria-label="Operazioni (schede)">
      {rows.map((t) => (
        <li key={t.key} className="rounded-lg border border-border p-3">
          <div className="flex items-start justify-between gap-2">
            <div>
              <Link to={detailLink(t)} className="font-semibold text-accent hover:underline">
                {t.symbol}
              </Link>
              <div className="text-xs text-muted">{`${t.portfolio_name} · ${t.strategy_ref ?? NO_STRATEGY}`}</div>
            </div>
            <OutcomeBadge outcome={t.outcome} />
          </div>
          <dl className="mt-2 grid grid-cols-2 gap-x-3 gap-y-1 text-sm">
            <dt className="text-muted">Aperta</dt>
            <dd className="num text-right">{formatDateTime(t.opened_at)}</dd>
            <dt className="text-muted">Ingresso</dt>
            <dd className="num text-right">{`${formatQty(t.qty)} × ${price(t.entry_price, t.currency)}`}</dd>
            <dt className="text-muted">Uscita</dt>
            <dd className="text-right">
              <Exit t={t} />
            </dd>
            <dt className="text-muted">Risultato</dt>
            <dd className="text-right">
              <Pnl value={t.pnl_eur} pct={t.pnl_pct} />
            </dd>
            <dt className="text-muted">Durata</dt>
            <dd className="num text-right">{formatHolding(t.holding_s)}</dd>
          </dl>
        </li>
      ))}
    </ul>
  );
}

/** «Diario operazioni»: every trade (FIFO round trip), filterable, newest first. */
export default function TradeJournal() {
  const [sp, setSp] = useSearchParams();
  const filters = filtersFrom(sp);
  const query = useTrades(filters);
  const data = query.data;
  const set = (key: keyof TradeFilters, value: string) =>
    setSp(
      (prev) => {
        const next = new URLSearchParams(prev);
        if (value) next.set(key, value);
        else next.delete(key);
        if (key !== "page") next.delete("page");
        return next;
      },
      { replace: true },
    );
  const pages = data ? Math.max(1, Math.ceil(data.total / TRADES_PAGE_SIZE)) : 1;
  const symbols = data?.filters.symbols ?? [];
  const s = data?.summary;
  return (
    <div>
      <PageHeader title="Diario operazioni">
        {data?.as_of && <span className="text-xs text-muted">{`ultimo aggiornamento ${formatDateTime(data.as_of)}`}</span>}
      </PageHeader>
      <Card>
        <CardContent className="space-y-4 pt-6">
          <div className="grid grid-cols-2 gap-3 sm:flex sm:flex-wrap sm:items-end">
            <div className="space-y-1">
              <Label htmlFor="trades-period">Periodo</Label>
              <select id="trades-period" className={`${selectClass} sm:w-44`} value={filters.period} onChange={(e) => set("period", e.target.value === "all" ? "" : e.target.value)}>
                {PERIODS.map((p) => (
                  <option key={p} value={p}>
                    {PERIOD_LABELS[p]}
                  </option>
                ))}
              </select>
            </div>
            <div className="space-y-1">
              <Label htmlFor="trades-portfolio">Portafoglio</Label>
              <select id="trades-portfolio" className={`${selectClass} sm:w-48`} value={filters.portfolio_id} onChange={(e) => set("portfolio_id", e.target.value)}>
                <option value="">Tutti</option>
                {data?.filters.portfolios.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.name}
                  </option>
                ))}
              </select>
            </div>
            <div className="space-y-1">
              <Label htmlFor="trades-strategy">Strategia</Label>
              <select id="trades-strategy" className={`${selectClass} sm:w-44`} value={filters.strategy} onChange={(e) => set("strategy", e.target.value)}>
                <option value="">Tutte</option>
                {data?.filters.strategies.map((st) => (
                  <option key={st.ref} value={st.ref}>
                    {st.ref}
                  </option>
                ))}
                {(data?.filters.no_strategy || filters.strategy === "none") && <option value="none">{NO_STRATEGY}</option>}
              </select>
            </div>
            <div className="space-y-1">
              <Label htmlFor="trades-symbol">Simbolo</Label>
              <select id="trades-symbol" className={`${selectClass} sm:w-36`} value={filters.symbol} onChange={(e) => set("symbol", e.target.value)}>
                <option value="">Tutti</option>
                {(filters.symbol && !symbols.includes(filters.symbol) ? [filters.symbol, ...symbols] : symbols).map((sym) => (
                  <option key={sym} value={sym}>
                    {sym}
                  </option>
                ))}
              </select>
            </div>
            <div className="space-y-1">
              <Label htmlFor="trades-outcome">Esito</Label>
              <select id="trades-outcome" className={`${selectClass} sm:w-36`} value={filters.outcome} onChange={(e) => set("outcome", e.target.value)}>
                <option value="">Tutti</option>
                {(Object.keys(OUTCOME_LABELS) as TradeOutcome[]).map((o) => (
                  <option key={o} value={o}>
                    {OUTCOME_LABELS[o]}
                  </option>
                ))}
              </select>
            </div>
          </div>
          {s && (
            <p className="text-sm" aria-label="Riepilogo">
              {`${s.trades} operazioni · ${s.open} aperte · ${s.closed} chiuse`}
              {s.win_rate_pct != null && ` · vincenti ${formatDecimal(s.win_rate_pct, 1)}% (${s.wins}/${s.closed})`}
              {" · risultato "}
              <Pnl value={s.pnl_eur} />
            </p>
          )}
          <p className="text-xs text-muted">
            Un'operazione va dall'ingresso alla chiusura dell'intera quantità; le uscite chiudono le entrate più vecchie (FIFO). Risultato in euro al cambio di oggi, al netto di commissioni e cambio.
          </p>
          {query.isError && <p className="text-sm text-loss">{errorMessage(query.error, "Impossibile caricare il diario")}</p>}
          {query.isLoading ? (
            <p className="text-sm text-muted">Caricamento…</p>
          ) : data && data.items.length === 0 ? (
            <p className="py-6 text-center text-sm text-muted">Nessuna operazione con questi filtri</p>
          ) : (
            data && (
              <>
                <div className="hidden md:block">
                  <TradesTable rows={data.items} />
                </div>
                <div className="md:hidden">
                  <TradeCards rows={data.items} />
                </div>
              </>
            )
          )}
          {pages > 1 && (
            <div className="flex items-center justify-center gap-3">
              <Button variant="outline" size="sm" disabled={filters.page <= 1} onClick={() => set("page", String(filters.page - 1))}>
                Precedenti
              </Button>
              <span className="text-sm text-muted">{`Pagina ${filters.page} di ${pages}`}</span>
              <Button variant="outline" size="sm" disabled={filters.page >= pages} onClick={() => set("page", String(filters.page + 1))}>
                Successive
              </Button>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
