import { useCallback, useMemo, useState } from "react";
import { EQUITY_HISTORY_RANGES, HOURLY_RANGES, useEquityHistory, type EquityHistoryRange } from "@/api/hooks/equityHistory";
import { EquityChart } from "@/components/charts";
import { Pnl } from "@/components/Pnl";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { historyLines, TOTAL_COLOR, TOTAL_KEY, totalChange, tooltipRows, type Unit } from "@/lib/equityHistory";
import { chartNumber, signedNumber } from "@/lib/lineChart";

const CHANGE_HINT = "variazione del valore (include depositi/prelievi)";

/** The price axis labels. */
const FORMAT: Record<Unit, (v: number) => string> = {
  eur: (v) => `${chartNumber(v, Math.abs(v) >= 1000 ? 0 : 2)} €`,
  pct: (v) => `${signedNumber(v, 2)}%`,
};

/**
 * «Andamento»: the equity over time. Dashboard (no `portfolioId`): every live portfolio,
 * in % since the range start by default so portfolios of any size compare, toggle €/%,
 * plus their «Totale» (bold, first in the legend) with more than one: the sum of their
 * equity, in % on the sum at the start, as the total goals compute it.
 * Portfolio detail: that portfolio only, in €, green when up over the range, red when down.
 * The chart (Lightweight Charts, lazily loaded) has the goals as labelled price lines and a
 * crosshair tooltip with every series' value and change; the legend stays below.
 */
export function EquityHistoryCard({ portfolioId }: { portfolioId?: string }) {
  const single = portfolioId != null;
  const [range, setRange] = useState<EquityHistoryRange>("1d");
  const [unit, setUnit] = useState<Unit>(single ? "eur" : "pct");
  const { data, isLoading, isError } = useEquityHistory(range, portfolioId);
  const lines = useMemo(() => (data ? historyLines(data, unit, single) : []), [data, unit, single]);
  // the tooltip shows euros and the change in both units, whatever the unit drawn
  const both = useMemo(() => (data ? { eur: historyLines(data, "eur", single), pct: historyLines(data, "pct", single) } : null), [data, single]);
  const rowsAt = useCallback((t: number) => (both ? tooltipRows(lines, both.eur, both.pct, t) : []), [lines, both]);
  const colors = new Map(lines.filter((l) => !l.reference && !l.thin).map((l) => [l.key, l.color]));
  const total = data && colors.has(TOTAL_KEY) ? totalChange(data) : null;
  const shown = data?.portfolios.filter((p) => colors.has(p.id)) ?? [];
  const goals = lines.filter((l) => l.reference);
  const indices = lines.filter((l) => l.thin);

  return (
    <Card>
      <CardHeader className="flex flex-row flex-wrap items-center justify-between gap-2 space-y-0">
        <CardTitle>Andamento</CardTitle>
        <div className="flex flex-wrap gap-2">
          <div className="flex gap-1" role="group" aria-label="Periodo">
            {EQUITY_HISTORY_RANGES.map((r) => (
              <Button
                key={r.value}
                size="sm"
                variant={r.value === range ? "secondary" : "ghost"}
                aria-pressed={r.value === range}
                onClick={() => setRange(r.value)}
              >
                {r.label}
              </Button>
            ))}
          </div>
          {!single && (
            <div className="flex gap-1" role="group" aria-label="Unità">
              {(["pct", "eur"] as const).map((u) => (
                <Button key={u} size="sm" variant={u === unit ? "secondary" : "ghost"} aria-pressed={u === unit} onClick={() => setUnit(u)}>
                  {u === "pct" ? "%" : "€"}
                </Button>
              ))}
            </div>
          )}
        </div>
      </CardHeader>
      <CardContent>
        {isError ? (
          <p className="py-16 text-center text-sm text-loss">Impossibile caricare l&apos;andamento</p>
        ) : isLoading || !data ? (
          <p className="py-16 text-center text-sm text-muted">Caricamento…</p>
        ) : lines.length === 0 ? (
          <p className="py-16 text-center text-sm text-muted">Nessun dato nel periodo</p>
        ) : (
          <>
            <EquityChart
              lines={lines}
              single={single}
              formatValue={FORMAT[unit]}
              rowsAt={rowsAt}
              daily={!HOURLY_RANGES.has(range)}
              resetKey={`${range}-${unit}`}
              ariaLabel={`Andamento ${single ? "del portafoglio" : "dei portafogli"} (${unit === "pct" ? "%" : "€"})`}
            />
            <ul className="mt-3 flex flex-wrap gap-x-5 gap-y-1 text-sm" aria-label="Legenda">
              {total && (
                <li className="flex items-center gap-1.5 font-medium" title={`somma dei portafogli conteggiati negli obiettivi · ${CHANGE_HINT}`}>
                  <span className="inline-block h-1 w-4 rounded-full" style={{ background: TOTAL_COLOR }} />
                  <span>{lines.find((l) => l.key === TOTAL_KEY)?.label ?? "Totale"}</span>
                  <Pnl value={total.change_eur} pct={total.change_pct} />
                </li>
              )}
              {shown.map((p) => (
                <li key={p.id} className="flex items-center gap-1.5" title={CHANGE_HINT}>
                  <span className="inline-block h-2.5 w-2.5 rounded-full" style={{ background: colors.get(p.id) }} />
                  <span>{p.name}</span>
                  <Pnl value={p.change_eur} pct={p.change_pct} />
                </li>
              ))}
              {goals.map((g) => (
                <li key={g.key} className="flex items-center gap-1.5 text-muted" data-testid={`legend-${g.key}`}>
                  <span className="inline-block w-4 border-t-2 border-dashed" style={{ borderColor: g.color }} />
                  {g.label}
                </li>
              ))}
              {indices.map((b) => (
                <li key={b.key} className="flex items-center gap-1.5 text-muted" title="in euro, sullo stesso periodo dell'obiettivo" data-testid={`legend-${b.key}`}>
                  <span className="inline-block w-4 border-t" style={{ borderColor: b.color }} />
                  {b.label}
                </li>
              ))}
              <li className="text-xs text-muted">{CHANGE_HINT}</li>
            </ul>
          </>
        )}
      </CardContent>
    </Card>
  );
}
