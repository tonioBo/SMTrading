import { ExternalLink } from "lucide-react";
import { useMemo, useState, type ReactNode } from "react";
import { useInstrumentMarket, useInstrumentOverview } from "@/api/hooks/marketsExplore";
import type { InstrumentMarket, InstrumentOverview } from "@/api/marketsTypes";
import { PriceChart } from "@/components/charts";
import type { PriceOverlay } from "@/components/charts/types";
import { Pct } from "@/components/Pct";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { formatClock, formatDateTime, formatDecimal, formatMoney, formatQty } from "@/lib/format";
import { ASSET_CLASS_LABELS } from "@/lib/labels";
import { chartMoney, fillMarkers, positionLines, sma, toCandles } from "@/lib/priceChart";
import { INDEX_LABELS } from "@/components/markets/labels";

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="space-y-2 border-t border-border pt-3" aria-label={title}>
      <h3 className="text-sm font-medium">{title}</h3>
      {children}
    </section>
  );
}

const none = <p className="text-sm text-muted">Nessuno</p>;
const fmtDay = (d: string) => d.split("-").reverse().join("/");
const num = (v: string | null | undefined, places = 2) => (v ? formatDecimal(v, places) : "—");

/** Price and day change: the quote (else the day change's price) against the previous close. */
function PriceLine({ m, currency }: { m: InstrumentMarket | undefined; currency: string }) {
  const last = m?.quote?.last ?? m?.day_change?.last ?? null;
  return (
    <div className="flex flex-wrap items-baseline gap-3">
      <span className="num text-2xl font-semibold">{last ? formatMoney(last, currency) : "—"}</span>
      <Pct value={m?.day_change?.change_pct ?? null} tone className="text-base" />
      {m?.day_change && <span className="text-xs text-muted">vs chiusura prec. {formatMoney(m.day_change.prev_close, currency)}</span>}
      {m?.quote?.ts && <span className="text-xs text-muted">{formatClock(m.quote.ts)} · {m.quote.provider}</span>}
    </div>
  );
}

const SMA_COLORS = { sma20: "#3b82f6", sma50: "#a855f7" } as const;

/**
 * The candlesticks of the sheet: volume below, SMA 20 / 50 on request, our average price,
 * stop and target as lines, our executions as markers.
 */
function Chart({ m, o, timeframe }: { m: InstrumentMarket | undefined; o: InstrumentOverview | undefined; timeframe: "1d" | "1h" }) {
  const [avg, setAvg] = useState<{ sma20: boolean; sma50: boolean }>({ sma20: false, sma50: false });
  const currency = o?.instrument.currency ?? "USD";
  const candles = useMemo(() => (m ? toCandles(m.bars) : []), [m]);
  const overlays = useMemo<PriceOverlay[]>(
    () =>
      (["sma20", "sma50"] as const)
        .filter((k) => avg[k])
        .map((k) => ({ key: k, label: k === "sma20" ? "SMA 20" : "SMA 50", color: SMA_COLORS[k], data: sma(candles, k === "sma20" ? 20 : 50) }))
        .filter((x) => x.data.length > 0),
    [avg, candles],
  );
  const lines = useMemo(() => (o ? positionLines(o.positions, o.open_orders, (v) => chartMoney(v, currency)) : []), [o, currency]);
  const markers = useMemo(
    () => fillMarkers((o?.fills ?? []).map((f) => ({ ts: f.ts, side: f.side, text: `${f.side === "buy" ? "Acquisto" : "Vendita"} ${formatQty(f.qty)}` })), candles, timeframe),
    [o, candles, timeframe],
  );
  if (!m) return <div className="h-64 animate-pulse rounded bg-card" />;
  if (candles.length < 2) return <p className="text-sm text-muted">{m.errors.bars ? "Grafico non disponibile" : "Nessun dato"}</p>;
  return (
    <div className="space-y-2">
      <PriceChart
        candles={candles}
        timeframe={timeframe}
        overlays={overlays}
        lines={lines}
        markers={markers}
        ariaLabel={timeframe === "1d" ? "Andamento giornaliero" : "Andamento orario"}
      />
      <div className="flex flex-wrap items-center gap-1 text-xs" role="group" aria-label="Medie mobili">
        {(["sma20", "sma50"] as const).map((k) => (
          <Button key={k} type="button" size="sm" variant={avg[k] ? "secondary" : "ghost"} aria-pressed={avg[k]} onClick={() => setAvg((a) => ({ ...a, [k]: !a[k] }))}>
            <span className="mr-1.5 inline-block h-0.5 w-3" style={{ background: SMA_COLORS[k] }} aria-hidden />
            {k === "sma20" ? "SMA 20" : "SMA 50"}
          </Button>
        ))}
        {markers.length > 0 && <span className="text-muted">▲ nostri acquisti · ▼ nostre vendite</span>}
      </div>
    </div>
  );
}

function Indicators({ m }: { m: InstrumentMarket | undefined }) {
  const ind = m?.indicators;
  if (!m) return null;
  if (!ind) return <p className="text-sm text-muted">Indicatori non disponibili</p>;
  const vs = (avg: string | null) => (avg && Number(ind.close) > 0 ? (Number(ind.close) >= Number(avg) ? "sopra" : "sotto") : null);
  const rsi = ind.rsi14 ? Number(ind.rsi14) : null;
  const cells: [string, string, string | null][] = [
    ["RSI 14", num(ind.rsi14, 1), rsi == null ? null : rsi >= 70 ? "ipercomprato" : rsi <= 30 ? "ipervenduto" : null],
    ["SMA 20", num(ind.sma20), vs(ind.sma20) && `prezzo ${vs(ind.sma20)}`],
    ["SMA 50", num(ind.sma50), vs(ind.sma50) && `prezzo ${vs(ind.sma50)}`],
    ["ATR 14", num(ind.atr14), ind.atr14 && Number(ind.close) > 0 ? `${formatDecimal(((Number(ind.atr14) / Number(ind.close)) * 100).toFixed(2), 2)}% del prezzo` : null],
  ];
  return (
    <dl className="grid grid-cols-2 gap-2 sm:grid-cols-4">
      {cells.map(([k, v, note]) => (
        <div key={k} className="rounded-md border border-border p-2">
          <dt className="text-xs text-muted">{k}</dt>
          <dd className="num font-medium">{v}</dd>
          {note && <dd className="text-xs text-muted">{note}</dd>}
        </div>
      ))}
      <p className="col-span-full text-xs text-muted">Su barre giornaliere chiuse al {formatDateTime(ind.ts).slice(0, 10)}</p>
    </dl>
  );
}

function Ours({ o }: { o: InstrumentOverview }) {
  return (
    <>
      <Section title="Le nostre posizioni">
        {o.positions.length === 0
          ? none
          : o.positions.map((p) => (
              <p key={p.portfolio_id} className="text-sm">
                <span className="font-medium">{p.portfolio_name}</span>: <span className="num">{formatQty(p.qty)}</span> a <span className="num">{formatMoney(p.avg_price, o.instrument.currency)}</span>
              </p>
            ))}
      </Section>
      <Section title="Ordini aperti">
        {o.open_orders.length === 0
          ? none
          : o.open_orders.map((x) => (
              <p key={x.id} className="text-sm">
                <span className="font-medium">{x.portfolio_name}</span>: {x.side === "buy" ? "acquisto" : "vendita"} {x.type}
                {x.qty && <span className="num"> {formatQty(x.qty)}</span>}
                {x.limit_price && <span className="num"> limite {formatMoney(x.limit_price, o.instrument.currency)}</span>}
                {x.stop_price && <span className="num"> stop {formatMoney(x.stop_price, o.instrument.currency)}</span>}
                <span className="text-xs text-muted"> · {x.status}</span>
              </p>
            ))}
      </Section>
      <Section title="Strategie">
        {o.strategies.length === 0
          ? none
          : o.strategies.map((s) => {
              const sig = o.signals?.find((x) => x.ref === s.ref);
              return (
                <div key={s.ref} className="text-sm">
                  <span className="num font-medium">{s.ref}</span> {s.name}
                  <span className="text-xs text-muted">
                    {" "}
                    · {[s.type, s.timeframe, s.portfolios.join(", ")].filter(Boolean).join(" · ")}
                  </span>
                  {sig?.triggered_now && <Badge className="ml-2 inline-flex">segnale attivo</Badge>}
                </div>
              );
            })}
      </Section>
      <Section title="Watchlist">
        {o.watchlists.length === 0 ? (
          none
        ) : (
          <div className="flex flex-wrap gap-1">
            {o.watchlists.map((w) => (
              <Badge key={`${w.agent}/${w.watchlist}`} variant="secondary">
                {w.agent}: {w.watchlist}
              </Badge>
            ))}
          </div>
        )}
      </Section>
    </>
  );
}

/** The instrument sheet: a side drawer on wide screens, full screen on a phone. */
export function InstrumentSheet({ symbol, onClose }: { symbol: string | null; onClose: () => void }) {
  const [timeframe, setTimeframe] = useState<"1d" | "1h">("1d");
  const overview = useInstrumentOverview(symbol);
  const market = useInstrumentMarket(symbol, timeframe);
  const o = overview.data;
  const m = market.data?.symbol === symbol ? market.data : undefined;
  const i = o?.instrument;
  return (
    <Dialog open={!!symbol} onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="left-auto right-0 top-0 h-full max-h-screen w-full max-w-full translate-x-0 translate-y-0 content-start overflow-y-auto sm:max-w-2xl sm:rounded-none">
        <DialogHeader className="pr-8 text-left">
          <DialogTitle className="flex flex-wrap items-baseline gap-2">
            <span className="num">{symbol}</span>
            <span className="text-base font-normal text-muted">{i?.name ?? ""}</span>
          </DialogTitle>
          <DialogDescription asChild>
            <div className="flex flex-wrap gap-1 text-xs">
              {i && <Badge variant="secondary">{ASSET_CLASS_LABELS[i.asset_class] ?? i.asset_class}</Badge>}
              {i && <Badge variant="secondary">{i.market}</Badge>}
              {i && <Badge variant="secondary">{i.currency}</Badge>}
              {o?.market_status && <Badge variant={o.market_status.is_open ? "default" : "secondary"}>{o.market_status.is_open ? "Mercato aperto" : "Mercato chiuso"}</Badge>}
              {i && !i.active && <Badge variant="secondary">disattivato</Badge>}
            </div>
          </DialogDescription>
        </DialogHeader>
        {overview.isError && <p className="text-loss">Impossibile caricare lo strumento</p>}
        <PriceLine m={m} currency={i?.currency ?? "USD"} />
        <div className="flex gap-1" role="group" aria-label="Intervallo del grafico">
          {(["1d", "1h"] as const).map((tf) => (
            <Button key={tf} type="button" size="sm" variant={tf === timeframe ? "default" : "outline"} aria-pressed={tf === timeframe} onClick={() => setTimeframe(tf)}>
              {tf === "1d" ? "Giornaliero" : "Orario"}
            </Button>
          ))}
        </div>
        <Chart m={m} o={o} timeframe={timeframe} />
        <Section title="Indicatori">
          <Indicators m={m} />
        </Section>
        {i && (
          <Section title="Settore e indici">
            <p className="text-sm">
              {i.sector ?? "Settore non indicato"}
              {i.industry && <span className="text-muted"> · {i.industry}</span>}
            </p>
            {(i.indices ?? []).length > 0 && (
              <div className="flex flex-wrap gap-1">
                {(i.indices ?? []).map((c) => (
                  <Badge key={c} variant="secondary">
                    {INDEX_LABELS[c] ?? c}
                  </Badge>
                ))}
              </div>
            )}
          </Section>
        )}
        <Section title="Prossimi eventi">
          {!m ? null : !m.next_earnings && !m.next_dividend ? (
            <p className="text-sm text-muted">{m.errors.calendar ? "Calendario non disponibile" : "Nessun evento nei prossimi mesi"}</p>
          ) : (
            <>
              {m.next_earnings && <p className="text-sm">Trimestrale: {fmtDay(m.next_earnings.date)}</p>}
              {m.next_dividend && <p className="text-sm">Dividendo: {fmtDay(m.next_dividend.date)}</p>}
            </>
          )}
        </Section>
        {o && <Ours o={o} />}
        <Section title="Notizie">
          {!m ? null : m.news.length === 0 ? (
            <p className="text-sm text-muted">{m.errors.news ? "Notizie non disponibili" : "Nessuna notizia recente"}</p>
          ) : (
            <ul className="space-y-2">
              {m.news.map((n) => (
                <li key={n.id} className="text-sm">
                  {n.url ? (
                    <a href={n.url} target="_blank" rel="noreferrer noopener" className="inline-flex items-start gap-1 hover:underline">
                      {n.headline}
                      <ExternalLink className="mt-0.5 h-3 w-3 shrink-0" aria-hidden="true" />
                    </a>
                  ) : (
                    n.headline
                  )}
                  <span className="block text-xs text-muted">
                    {formatDateTime(n.ts)} · {n.source}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </Section>
      </DialogContent>
    </Dialog>
  );
}
