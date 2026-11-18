import { useState } from "react";
import { EQUITY_HISTORY_RANGES, useEquityHistory, type EquityHistoryRange } from "@/api/hooks/equityHistory";
import { LineChart } from "@/components/LineChart";
import { Pnl } from "@/components/Pnl";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { historyLines, TOTAL_COLOR, TOTAL_KEY, totalChange, type Unit } from "@/lib/equityHistory";
import { chartNumber } from "@/lib/lineChart";

function signed(v: number, places: number): string {
  const text = chartNumber(v, places);
  return (v > 0 && /[1-9]/.test(text) ? "+" : "") + text;
}

const CHANGE_HINT = "variazione del valore (include depositi/prelievi)";

const FORMAT: Record<Unit, { value: (v: number) => string; tick: (v: number, step: number) => string }> = {
  eur: {
    value: (v) => `${chartNumber(v, 2)} €`,
    tick: (v, step) => `${chartNumber(v, step >= 1 ? 0 : 2)} €`,
  },
  pct: {
    value: (v) => `${signed(v, 2)}%`,
    tick: (v, step) => `${signed(v, step >= 1 ? 0 : step >= 0.1 ? 1 : 2)}%`,
  },
};

/**
 * «Andamento»: the equity over time. Dashboard (no `portfolioId`): every live portfolio,
 * in % since the range start by default so portfolios of any size compare, toggle €/%,
 * plus their «Totale» (bold, first in the legend) with more than one: the sum of their
 * equity, in % on the sum at the start, as the total goals compute it.
 * Portfolio detail: that portfolio only, in €.
 */
export function EquityHistoryCard({ portfolioId }: { portfolioId?: string }) {
  const single = portfolioId != null;
  const [range, setRange] = useState<EquityHistoryRange>("1w");
  const [unit, setUnit] = useState<Unit>(single ? "eur" : "pct");
  const { data, isLoading, isError } = useEquityHistory(range, portfolioId);
  const lines = data ? historyLines(data, unit, single) : [];
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
            <LineChart
              lines={lines}
              formatValue={FORMAT[unit].value}
              formatTick={FORMAT[unit].tick}
              axisTime={range === "1d" ? "time" : "day"}
              ariaLabel={`Andamento ${single ? "del portafoglio" : "dei portafogli"} (${unit === "pct" ? "%" : "€"})`}
            />
            <ul className="mt-3 flex flex-wrap gap-x-5 gap-y-1 text-sm" aria-label="Legenda">
              {total && (
                <li className="flex items-center gap-1.5 font-medium" title={`somma dei portafogli · ${CHANGE_HINT}`}>
                  <span className="inline-block h-1 w-4 rounded-full" style={{ background: TOTAL_COLOR }} />
                  <span>Totale</span>
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
                <li key={g.key} className="flex items-center gap-1.5 text-muted">
                  <span className="inline-block w-4 border-t-2 border-dashed" style={{ borderColor: g.color }} />
                  {g.label}
                </li>
              ))}
              {indices.map((b) => (
                <li key={b.key} className="flex items-center gap-1.5 text-muted" title="in euro, sullo stesso periodo dell'obiettivo">
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
