import { useEffect, useState } from "react";
import { useOverview, useOverviewChart } from "@/api/hooks/marketsV2";
import type { OverviewItem, OverviewRange } from "@/api/marketsV2Types";
import { OverviewChart } from "@/components/charts";
import { Pct } from "@/components/Pct";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { formatClock, formatDecimal } from "@/lib/format";
import { marketLine, overviewValue, sparkPoints } from "@/lib/marketsV2";
import { cn } from "@/lib/utils";

const RANGES: { code: OverviewRange; label: string }[] = [
  { code: "1d", label: "1g" },
  { code: "5d", label: "5g" },
  { code: "1mo", label: "1m" },
  { code: "1y", label: "1a" },
];

/** The clock of the countdowns: ticks every 30 s. */
function useNow(ms = 30_000): Date {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const t = setInterval(() => setNow(new Date()), ms);
    return () => clearInterval(t);
  }, [ms]);
  return now;
}

function Spark({ item }: { item: OverviewItem }) {
  const pts = sparkPoints(item.spark, 96, 28);
  if (!pts) return <div className="h-7" aria-hidden />;
  const up = Number(item.change_pct ?? 0) >= 0;
  return (
    <svg viewBox="0 0 96 28" className="h-7 w-full" preserveAspectRatio="none" aria-hidden>
      <polyline points={pts} fill="none" strokeWidth="1.5" className={up ? "stroke-gain" : "stroke-loss"} vectorEffect="non-scaling-stroke" />
    </svg>
  );
}

function Tile({ item, now, onOpen }: { item: OverviewItem; now: Date; onOpen: (i: OverviewItem) => void }) {
  const line = marketLine(item, now);
  return (
    <button
      type="button"
      onClick={() => onOpen(item)}
      aria-label={`${item.label}: ${overviewValue(item)}${item.change_pct ? `, ${item.change_pct}%` : ""}`}
      className="flex w-40 shrink-0 snap-start flex-col gap-0.5 rounded-md border border-border bg-card/40 p-2 text-left hover:bg-card sm:w-auto"
    >
      <span className="flex items-center gap-1.5 text-xs text-muted">
        <span className={cn("inline-block h-1.5 w-1.5 rounded-full", item.is_open ? "bg-gain" : "bg-muted/60")} aria-hidden />
        <span className="truncate font-medium text-fg">{item.label}</span>
      </span>
      <span className="num text-base font-semibold leading-tight">{overviewValue(item)}</span>
      <Pct value={item.change_pct} tone className="text-xs" />
      <Spark item={item} />
      <span className="truncate text-[11px] text-muted">{line}</span>
    </button>
  );
}

function ChartDialog({ item, onClose }: { item: OverviewItem; onClose: () => void }) {
  const [range, setRange] = useState<OverviewRange>("1d");
  const chart = useOverviewChart(item.code, range);
  const data = chart.data?.code === item.code && chart.data.range === range ? chart.data : undefined;
  const fmt = (v: number) => formatDecimal(v.toFixed(Math.min(item.decimals, 6)), item.decimals) + (item.unit === "%" ? "%" : "");
  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="max-h-[95vh] overflow-y-auto sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle className="flex flex-wrap items-baseline gap-2">
            {item.label}
            <span className="num text-base font-normal">{overviewValue(item)}</span>
            <Pct value={item.change_pct} tone className="text-sm" />
          </DialogTitle>
          <DialogDescription>
            {item.symbol} · {item.group}
            {item.note ? ` · ${item.note}` : ""}
            {item.ts ? ` · ultimo valore ${formatClock(item.ts)}` : ""}
          </DialogDescription>
        </DialogHeader>
        <div className="flex gap-1" role="group" aria-label="Periodo del grafico">
          {RANGES.map((r) => (
            <Button key={r.code} type="button" size="sm" variant={r.code === range ? "default" : "outline"} aria-pressed={r.code === range} onClick={() => setRange(r.code)}>
              {r.label}
            </Button>
          ))}
        </div>
        {!data ? (
          <div className="h-[220px] animate-pulse rounded-md bg-card sm:h-[280px]" />
        ) : data.points.length < 2 ? (
          <p className="py-10 text-center text-sm text-muted">{data.error ? "Grafico non disponibile" : "Nessun dato"}</p>
        ) : (
          <OverviewChart points={data.points} daily={range === "1mo" || range === "1y"} formatValue={fmt} ariaLabel={`Andamento ${item.label}`} />
        )}
      </DialogContent>
    </Dialog>
  );
}

/**
 * «Mercati oggi»: the real indices, Bitcoin, EUR/USD, gold, oil and the 10-year yield —
 * value, day change, a sparkline (intraday when the market is open, else 5 days) and the
 * market's state with a countdown. A row that scrolls sideways on a phone, a grid on wide
 * screens; a tile opens its chart (1g / 5g / 1m / 1a).
 */
export function MarketsToday() {
  const { data, isError, isLoading } = useOverview();
  const now = useNow();
  const [open, setOpen] = useState<OverviewItem | null>(null);
  return (
    <section aria-label="Mercati oggi" className="space-y-2">
      <div className="flex items-baseline justify-between gap-2">
        <h2 className="text-sm font-semibold">Mercati oggi</h2>
        {data && (
          <span className="text-xs text-muted">
            {data.stale ? "Dati non aggiornati · " : ""}agg. {formatClock(data.as_of)}
          </span>
        )}
      </div>
      {isError && <p className="text-sm text-loss">Impossibile caricare i mercati</p>}
      {isLoading && <div className="h-28 animate-pulse rounded-md bg-card" />}
      {data?.error && <p className="text-sm text-muted">Dati di mercato non disponibili al momento</p>}
      {data && !data.error && (
        <div className="-mx-3 flex snap-x gap-2 overflow-x-auto px-3 pb-1 sm:mx-0 sm:grid sm:grid-cols-3 sm:overflow-visible sm:px-0 md:grid-cols-5 xl:grid-cols-8" role="list">
          {data.items.map((it) => (
            <div role="listitem" key={it.code} className="contents">
              <Tile item={it} now={now} onOpen={setOpen} />
            </div>
          ))}
        </div>
      )}
      {open && <ChartDialog item={open} onClose={() => setOpen(null)} />}
    </section>
  );
}
