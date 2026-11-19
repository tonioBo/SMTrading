import { ArrowLeft } from "lucide-react";
import type { ReactNode } from "react";
import { Link, useParams } from "react-router-dom";
import { useTrade } from "@/api/hooks/trades";
import type { TradeDecision, TradeFill, TradeOrder, TradeTimelineItem } from "@/api/types";
import { Money } from "@/components/Money";
import { OutcomeBadge } from "@/components/OutcomeBadge";
import { PageHeader } from "@/components/PageHeader";
import { Pnl } from "@/components/Pnl";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { errorMessage } from "@/lib/errors";
import { formatDateTime, formatHolding, formatMoney, formatQty } from "@/lib/format";
import { cn } from "@/lib/utils";
import { NO_STRATEGY } from "@/lib/trades";

const SIDE_LABELS: Record<string, string> = { buy: "acquisto", sell: "vendita" };
const STATUS_LABELS: Record<string, string> = {
  new: "nuovo",
  held: "in attesa dell'ingresso",
  queued: "in coda",
  pending: "attivo",
  triggered: "scattato",
  partially_filled: "eseguito in parte",
  filled: "eseguito",
  canceled: "annullato",
  rejected: "rifiutato",
  expired: "scaduto",
};
const TYPE_LABELS: Record<string, string> = {
  market: "a mercato",
  limit: "limite",
  stop: "stop",
  stop_limit: "stop limite",
  trailing_stop: "stop mobile",
};
const SOURCE_LABELS: Record<string, string> = { agent: "agenti", admin: "amministratori", system: "sistema" };

function money(v: string | null, currency: string): string {
  return v == null ? "—" : formatMoney(v, currency);
}

function dot(type: string): string {
  if (type.startsWith("fill.")) return "bg-gain";
  if (type === "lesson" || type === "decision.veto" || type.endsWith(".rejected")) return "bg-loss";
  if (type.startsWith("decision.")) return "bg-accent";
  if (type.endsWith(".canceled") || type.endsWith(".expired")) return "bg-warn";
  return "bg-muted";
}

function Timeline({ items }: { items: TradeTimelineItem[] }) {
  if (items.length === 0) return <p className="text-sm text-muted">Nessun evento</p>;
  return (
    <ol className="relative space-y-4 border-l border-border pl-5" aria-label="Cronologia">
      {items.map((i, n) => (
        <li key={`${i.type}-${i.ts}-${n}`} className="relative">
          <span className={cn("absolute -left-[26px] top-1.5 h-2.5 w-2.5 rounded-full", dot(i.type))} aria-hidden />
          <div className="flex flex-wrap items-baseline gap-x-2">
            <span className="font-medium">{i.title}</span>
            <span className="num text-xs text-muted">{formatDateTime(i.ts)}</span>
          </div>
          {i.text && <p className="break-words text-sm text-muted">{i.text}</p>}
        </li>
      ))}
    </ol>
  );
}

function value(v: unknown): string {
  if (v == null) return "—";
  return typeof v === "string" || typeof v === "number" || typeof v === "boolean" ? String(v) : JSON.stringify(v);
}

/** The proposal's details (signal, indicator values, stop and target…). */
function Details({ d }: { d: TradeDecision }) {
  const entries = Object.entries(d.details);
  return (
    <div className="space-y-2 text-sm">
      <p>{d.summary}</p>
      <p className="text-xs text-muted">{`${d.agent ?? "agente sconosciuto"} · ${formatDateTime(d.created_at)}`}</p>
      {entries.length > 0 && (
        <dl className="grid grid-cols-1 gap-x-4 gap-y-1 sm:grid-cols-[max-content_1fr]">
          {entries.map(([k, v]) => (
            <div key={k} className="contents">
              <dt className="font-mono text-xs text-muted">{k}</dt>
              <dd className="break-all font-mono text-xs">{value(v)}</dd>
            </div>
          ))}
        </dl>
      )}
    </div>
  );
}

function OrdersTable({ orders }: { orders: TradeOrder[] }) {
  return (
    <div className="overflow-x-auto">
      <Table aria-label="Ordini">
        <TableHeader>
          <TableRow>
            <TableHead>Ordine</TableHead>
            <TableHead>Creato</TableHead>
            <TableHead>Lato</TableHead>
            <TableHead className="text-right">Quantità</TableHead>
            <TableHead className="text-right">Prezzo</TableHead>
            <TableHead>Stato</TableHead>
            <TableHead>Da</TableHead>
            <TableHead>Note</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {orders.map((o) => (
            <TableRow key={o.id}>
              <TableCell>
                <div className="font-medium">{o.label}</div>
                <div className="num text-xs text-muted">{o.client_order_id ?? o.id.slice(0, 8)}</div>
              </TableCell>
              <TableCell className="num whitespace-nowrap">{formatDateTime(o.created_at)}</TableCell>
              <TableCell>{`${SIDE_LABELS[o.side] ?? o.side} ${TYPE_LABELS[o.type] ?? o.type}`}</TableCell>
              <TableCell className="num text-right">{o.qty == null ? "—" : formatQty(o.qty)}</TableCell>
              <TableCell className="num text-right">{o.limit_price ?? o.stop_price ?? (o.avg_fill_price ? formatQty(o.avg_fill_price) : "—")}</TableCell>
              <TableCell>{STATUS_LABELS[o.status] ?? o.status}</TableCell>
              <TableCell>{o.agent ?? SOURCE_LABELS[o.source] ?? o.source}</TableCell>
              <TableCell className="text-xs text-muted">{o.cancel_reason ?? o.reject_message ?? ""}</TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}

function fx(f: TradeFill): string {
  if (!f.fx || !f.fx.sold || !f.fx.bought) return "—";
  const fee = Object.entries(f.fx.fee)
    .map(([c, a]) => formatMoney(a, c))
    .join(", ");
  const rate = f.fx.rate ? ` a ${f.fx.rate}` : "";
  return `${formatMoney(f.fx.sold.amount, f.fx.sold.currency)} → ${formatMoney(f.fx.bought.amount, f.fx.bought.currency)}${rate}${fee ? ` (costo ${fee})` : ""}`;
}

function FillsTable({ fills }: { fills: TradeFill[] }) {
  return (
    <div className="overflow-x-auto">
      <Table aria-label="Esecuzioni">
        <TableHeader>
          <TableRow>
            <TableHead>Quando</TableHead>
            <TableHead>Tipo</TableHead>
            <TableHead className="text-right">Quantità</TableHead>
            <TableHead className="text-right">Prezzo</TableHead>
            <TableHead className="text-right">Commissione</TableHead>
            <TableHead>Cambio</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {fills.map((f, n) => (
            <TableRow key={`${f.fill_id}-${n}`}>
              <TableCell className="num whitespace-nowrap">{formatDateTime(f.ts)}</TableCell>
              <TableCell>{f.kind === "entry" ? "Ingresso" : `Uscita${f.exit_reason_label ? ` · ${f.exit_reason_label}` : ""}`}</TableCell>
              <TableCell className="num text-right">
                {formatQty(f.qty)}
                {f.qty !== f.fill_qty && <span className="ml-1 text-xs text-muted">{`di ${formatQty(f.fill_qty)}`}</span>}
              </TableCell>
              <TableCell className="num text-right">{formatMoney(f.price, f.currency)}</TableCell>
              <TableCell className="text-right">{f.fee_amount && f.fee_currency ? <Money value={f.fee_amount} currency={f.fee_currency} /> : "—"}</TableCell>
              <TableCell className="text-xs">{fx(f)}</TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}

function Fact({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div>
      <dt className="text-xs text-muted">{label}</dt>
      <dd className="font-medium">{children}</dd>
    </div>
  );
}

/** One trade of the «Diario operazioni»: summary, timeline, orders, fills, lessons. */
export default function TradeDetail() {
  const { key = "" } = useParams();
  const { data, isError, error, isLoading } = useTrade(key);
  const t = data?.trade;
  return (
    <div>
      <Link to="/diario-operazioni" aria-label="Torna al diario operazioni" className="mb-3 inline-flex items-center gap-1 text-sm text-accent hover:underline">
        <ArrowLeft className="h-4 w-4" aria-hidden />
        Diario operazioni
      </Link>
      <PageHeader title={t ? `Operazione ${t.symbol}` : "Operazione"}>{t && <OutcomeBadge outcome={t.outcome} />}</PageHeader>
      {isError && <p className="text-loss">{errorMessage(error, "Impossibile caricare l'operazione")}</p>}
      {isLoading && <p className="text-sm text-muted">Caricamento…</p>}
      {data && t && (
        <div className="space-y-6">
          <Card>
            <CardContent className="pt-6">
              <dl className="grid grid-cols-2 gap-4 text-sm sm:grid-cols-3 lg:grid-cols-6">
                <Fact label="Strumento">{data.instrument_name ? `${t.symbol} · ${data.instrument_name}` : t.symbol}</Fact>
                <Fact label="Portafoglio">
                  <Link to={`/portafogli/${t.portfolio_id}`} className="text-accent hover:underline">
                    {t.portfolio_name}
                  </Link>
                </Fact>
                <Fact label="Strategia">{t.strategy_ref ? `${t.strategy_ref}${t.strategy_name ? ` · ${t.strategy_name}` : ""}` : NO_STRATEGY}</Fact>
                <Fact label="Proposta">{t.proposal_ref ?? "—"}</Fact>
                <Fact label="Ingresso">{`${formatQty(t.qty)} × ${money(t.entry_price, t.currency)}`}</Fact>
                <Fact label={t.closed_at ? "Uscita" : "Prezzo attuale"}>{t.closed_at ? money(t.exit_price, t.currency) : money(t.mark, t.currency)}</Fact>
                <Fact label="Aperta">{formatDateTime(t.opened_at)}</Fact>
                <Fact label="Chiusa">{t.closed_at ? formatDateTime(t.closed_at) : "ancora aperta"}</Fact>
                <Fact label="Durata">{formatHolding(t.holding_s)}</Fact>
                <Fact label="Motivo d'uscita">{t.exit_reason_label ?? "—"}</Fact>
                <Fact label="Risultato">
                  <Pnl value={t.pnl_eur} pct={t.pnl_pct} />
                </Fact>
                <Fact label="Costi">
                  <Money value={t.costs_eur} />
                </Fact>
              </dl>
            </CardContent>
          </Card>
          <div className="grid gap-6 lg:grid-cols-2">
            <Card>
              <CardHeader>
                <CardTitle>Cronologia</CardTitle>
              </CardHeader>
              <CardContent>
                <Timeline items={data.timeline} />
              </CardContent>
            </Card>
            <div className="space-y-6">
              <Card>
                <CardHeader>
                  <CardTitle>Segnale della proposta</CardTitle>
                </CardHeader>
                <CardContent>{data.proposal ? <Details d={data.proposal} /> : <p className="text-sm text-muted">Nessuna proposta registrata (ordine manuale o senza riferimento)</p>}</CardContent>
              </Card>
              <Card>
                <CardHeader>
                  <CardTitle>Lezioni collegate</CardTitle>
                </CardHeader>
                <CardContent>
                  {data.lessons.length === 0 ? (
                    <p className="text-sm text-muted">Nessuna lezione</p>
                  ) : (
                    <ul className="space-y-2 text-sm" aria-label="Lezioni">
                      {data.lessons.map((l) => (
                        <li key={l.id}>
                          <div>{l.summary}</div>
                          <div className="text-xs text-muted">{`${l.agent ?? "—"} · ${formatDateTime(l.created_at)}`}</div>
                        </li>
                      ))}
                    </ul>
                  )}
                </CardContent>
              </Card>
            </div>
          </div>
          <Card>
            <CardHeader>
              <CardTitle>Ordini</CardTitle>
            </CardHeader>
            <CardContent>
              <OrdersTable orders={data.orders} />
            </CardContent>
          </Card>
          <Card>
            <CardHeader>
              <CardTitle>Esecuzioni</CardTitle>
            </CardHeader>
            <CardContent>
              <FillsTable fills={data.fills} />
            </CardContent>
          </Card>
        </div>
      )}
    </div>
  );
}
