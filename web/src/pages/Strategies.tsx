import { Fragment, useState } from "react";
import { Link } from "react-router-dom";
import { useStrategiesPerformance } from "@/api/hooks/strategies";
import type { StrategyPerformance, StrategyTrade } from "@/api/types";
import { Money } from "@/components/Money";
import { PageHeader } from "@/components/PageHeader";
import { Pnl } from "@/components/Pnl";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { formatDateTime, formatDecimal, formatMoney, formatQty } from "@/lib/format";

export const NO_STRATEGY = "Senza strategia";

const STATUS_VARIANT: Record<string, "secondary" | "warning" | "outline"> = {
  approvata: "secondary",
  modificata: "secondary",
  sospesa: "warning",
  revocata: "outline",
  respinta: "outline",
};

function rowKey(s: StrategyPerformance): string {
  return `${s.ref ?? "none"}:${s.portfolio_id}`;
}

function price(v: string | null, currency: string): string {
  return v == null ? "—" : formatMoney(v, currency);
}

function TradesTable({ s }: { s: StrategyPerformance }) {
  const label = `Operazioni ${s.ref ?? NO_STRATEGY} ${s.portfolio_name}`;
  return (
    <Table aria-label={label}>
      <TableHeader>
        <TableRow>
          <TableHead>Simbolo</TableHead>
          <TableHead>Verso</TableHead>
          <TableHead>Aperta</TableHead>
          <TableHead>Chiusa</TableHead>
          <TableHead className="text-right">Quantità</TableHead>
          <TableHead className="text-right">Ingresso</TableHead>
          <TableHead className="text-right">Uscita / attuale</TableHead>
          <TableHead className="text-right">Realizzato</TableHead>
          <TableHead className="text-right">Non realizzato</TableHead>
          <TableHead className="text-right">Costi</TableHead>
          <TableHead className="text-right">Risultato</TableHead>
          <TableHead>Proposta</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {s.trade_list.map((t: StrategyTrade) => (
          <TableRow key={t.order_id}>
            <TableCell className="font-medium">{t.symbol}</TableCell>
            <TableCell>{t.direction === "long" ? "Rialzo" : "Ribasso"}</TableCell>
            <TableCell className="whitespace-nowrap">{formatDateTime(t.opened_at)}</TableCell>
            <TableCell className="whitespace-nowrap">{t.closed_at ? formatDateTime(t.closed_at) : <Badge variant="outline">aperta</Badge>}</TableCell>
            <TableCell className="num text-right">
              {formatQty(t.qty)}
              {t.open_qty !== "0" && t.open_qty !== t.qty && <span className="ml-1 text-xs text-muted">{`(aperte ${formatQty(t.open_qty)})`}</span>}
            </TableCell>
            <TableCell className="num text-right">{price(t.entry_price, t.currency)}</TableCell>
            <TableCell className="num text-right">{t.closed_at ? price(t.exit_price, t.currency) : price(t.mark, t.currency)}</TableCell>
            <TableCell className="text-right"><Pnl value={t.realized_pnl_eur} /></TableCell>
            <TableCell className="text-right"><Pnl value={t.unrealized_pnl_eur} /></TableCell>
            <TableCell className="text-right"><Money value={t.costs_eur} /></TableCell>
            <TableCell className="text-right"><Pnl value={t.pnl_eur} /></TableCell>
            <TableCell className="num text-xs text-muted">{t.client_order_id ?? "—"}</TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}

function StrategyRow({ s, open, onToggle }: { s: StrategyPerformance; open: boolean; onToggle: () => void }) {
  const title = s.ref ?? NO_STRATEGY;
  return (
    <Fragment>
      <TableRow>
        <TableCell>
          <div className="num font-semibold">{title}</div>
          {s.name && <div className="text-xs text-muted">{s.name}</div>}
        </TableCell>
        <TableCell>
          <Link to={`/portafogli/${s.portfolio_id}`} className="text-accent hover:underline">
            {s.portfolio_name}
          </Link>
        </TableCell>
        <TableCell>{s.status ? <Badge variant={STATUS_VARIANT[s.status] ?? "outline"}>{s.status}</Badge> : <span className="text-muted">—</span>}</TableCell>
        <TableCell className="num text-right">{s.trades}</TableCell>
        <TableCell className="num text-right">{s.open_positions}</TableCell>
        <TableCell className="num text-right">{s.win_rate_pct == null ? "—" : `${formatDecimal(s.win_rate_pct, 1)}%`}</TableCell>
        <TableCell className="text-right"><Pnl value={s.realized_pnl_eur} /></TableCell>
        <TableCell className="text-right"><Pnl value={s.unrealized_pnl_eur} /></TableCell>
        <TableCell className="text-right"><Money value={s.costs_eur} /></TableCell>
        <TableCell className="text-right font-semibold"><Pnl value={s.total_pnl_eur} /></TableCell>
        <TableCell className="whitespace-nowrap text-xs text-muted">
          {`${formatDateTime(s.first_trade_at)} – ${formatDateTime(s.last_trade_at)}`}
        </TableCell>
        <TableCell>
          <Button variant="ghost" size="sm" aria-expanded={open} aria-label={`Dettaglio ${title} ${s.portfolio_name}`} onClick={onToggle}>
            {open ? "Chiudi" : "Dettaglio"}
          </Button>
        </TableCell>
      </TableRow>
      {open && (
        <TableRow>
          <TableCell colSpan={12} className="bg-card/50">
            <TradesTable s={s} />
          </TableCell>
        </TableRow>
      )}
    </Fragment>
  );
}

export default function Strategies() {
  const { data, isError, isLoading } = useStrategiesPerformance();
  const [open, setOpen] = useState<Set<string>>(new Set());
  const rows = data?.strategies ?? [];
  const toggle = (key: string) =>
    setOpen((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  return (
    <div>
      <PageHeader title="Strategie">
        {data?.as_of && <span className="text-xs text-muted">{`ultimo aggiornamento ${formatDateTime(data.as_of)}`}</span>}
      </PageHeader>
      {isError && <p className="text-loss">Impossibile caricare i risultati delle strategie</p>}
      {isLoading ? (
        <p className="text-sm text-muted">Caricamento…</p>
      ) : (
        data &&
        (rows.length === 0 ? (
          <p className="text-sm text-muted">Nessuna operazione eseguita</p>
        ) : (
          <Card>
            <CardContent className="pt-6">
              <p className="mb-3 text-xs text-muted">
                Risultati lordi in euro; i costi (commissioni e cambio) sono a parte e già tolti dal totale. Le uscite chiudono le entrate più vecchie (FIFO).
              </p>
              <Table aria-label="Risultati per strategia">
                <TableHeader>
                  <TableRow>
                    <TableHead>Strategia</TableHead>
                    <TableHead>Portafoglio</TableHead>
                    <TableHead>Stato</TableHead>
                    <TableHead className="text-right">Operazioni</TableHead>
                    <TableHead className="text-right">Posizioni aperte</TableHead>
                    <TableHead className="text-right">Vincenti</TableHead>
                    <TableHead className="text-right">Realizzato</TableHead>
                    <TableHead className="text-right">Non realizzato</TableHead>
                    <TableHead className="text-right">Costi</TableHead>
                    <TableHead className="text-right">Totale</TableHead>
                    <TableHead>Periodo</TableHead>
                    <TableHead />
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {rows.map((s) => (
                    <StrategyRow key={rowKey(s)} s={s} open={open.has(rowKey(s))} onToggle={() => toggle(rowKey(s))} />
                  ))}
                </TableBody>
              </Table>
            </CardContent>
          </Card>
        ))
      )}
    </div>
  );
}
