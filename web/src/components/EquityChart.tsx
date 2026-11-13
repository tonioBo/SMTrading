import { ColorType, createChart, type IChartApi, type ISeriesApi, type UTCTimestamp } from "lightweight-charts";
import { useEffect, useRef, useState } from "react";
import { EQUITY_PERIODS as PERIODS, useEquity } from "@/api/hooks/dashboard";
import { usePerformance } from "@/api/hooks/portfolios";
import type { EquityPeriod, EquityPoint } from "@/api/types";
import { Button } from "@/components/ui/button";
import { cssColor } from "@/lib/cssColor";

interface SeriesQuery {
  data?: { series: EquityPoint[] };
  isLoading: boolean;
  isError: boolean;
}

function AggregateSeries({ period }: { period: EquityPeriod }) {
  return <SeriesView query={useEquity(period)} />;
}

function PortfolioSeries({ portfolioId, period }: { portfolioId: string; period: EquityPeriod }) {
  return <SeriesView query={usePerformance(portfolioId, period)} />;
}

/**
 * Equity curve: the aggregated one, or one portfolio's (`portfolioId`). Values become
 * numbers only here, for the chart: labels use format.ts.
 */
export function EquityChart({ portfolioId }: { portfolioId?: string } = {}) {
  const [period, setPeriod] = useState<EquityPeriod>("1m");
  return (
    <div>
      <div className="mb-3 flex gap-1" role="group" aria-label="Periodo">
        {PERIODS.map((p) => (
          <Button
            key={p.value}
            size="sm"
            variant={p.value === period ? "secondary" : "ghost"}
            aria-pressed={p.value === period}
            onClick={() => setPeriod(p.value)}
          >
            {p.label}
          </Button>
        ))}
      </div>
      {portfolioId ? <PortfolioSeries portfolioId={portfolioId} period={period} /> : <AggregateSeries period={period} />}
    </div>
  );
}

function SeriesView({ query }: { query: SeriesQuery }) {
  const { data, isLoading, isError } = query;
  const host = useRef<HTMLDivElement>(null);
  const chart = useRef<IChartApi | null>(null);
  const series = useRef<ISeriesApi<"Area"> | null>(null);
  const empty = !isLoading && !isError && (data?.series.length ?? 0) === 0;

  useEffect(() => {
    if (!host.current || empty) return;
    const accent = cssColor("--accent", "#10b981");
    const api = createChart(host.current, {
      autoSize: true,
      layout: {
        background: { type: ColorType.Solid, color: "transparent" },
        textColor: cssColor("--muted", "#a1a1aa"),
      },
      grid: { vertLines: { visible: false }, horzLines: { color: cssColor("--border", "#27272a") } },
      localization: { locale: "it-IT" },
      timeScale: { timeVisible: true },
    });
    chart.current = api;
    series.current = api.addAreaSeries({ lineColor: accent, topColor: accent, bottomColor: "transparent" });
    return () => {
      api.remove();
      chart.current = null;
      series.current = null;
    };
  }, [empty]);

  useEffect(() => {
    if (!data || !series.current) return;
    series.current.setData(
      data.series.map((p) => ({
        time: Math.floor(new Date(p.ts).getTime() / 1000) as UTCTimestamp,
        value: Number(p.equity_eur),
      })),
    );
    chart.current?.timeScale().fitContent();
  }, [data, empty]);

  return isError ? (
    <p className="py-16 text-center text-sm text-loss">Impossibile caricare il grafico</p>
  ) : empty ? (
    <p className="py-16 text-center text-sm text-muted">Nessun dato</p>
  ) : (
    <div ref={host} className="h-72 w-full" data-testid="equity-chart" />
  );
}
