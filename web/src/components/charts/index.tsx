/**
 * The charts, loaded on first use (their libraries stay out of the first page load):
 * TradingView Lightweight Charts™ for prices and equity, Apache ECharts for the treemap.
 * While a module loads, a placeholder of the chart's size keeps the layout still.
 */
import { lazy, Suspense } from "react";
import type { EquityChartProps, PriceChartProps, SectorTreemapProps } from "@/components/charts/types";

const EquityImpl = lazy(() => import("@/components/charts/EquityChart"));
const PriceImpl = lazy(() => import("@/components/charts/PriceChart"));
const TreemapImpl = lazy(() => import("@/components/charts/SectorTreemap"));

function Placeholder({ className }: { className: string }) {
  return <div className={`w-full animate-pulse rounded-md bg-card ${className}`} aria-busy="true" aria-label="Caricamento del grafico" />;
}

export function EquityChart(props: EquityChartProps) {
  return (
    <Suspense fallback={<Placeholder className="h-60 sm:h-[300px]" />}>
      <EquityImpl {...props} />
    </Suspense>
  );
}

export function PriceChart(props: PriceChartProps) {
  return (
    <Suspense fallback={<Placeholder className="h-[366px] sm:h-[446px]" />}>
      <PriceImpl {...props} />
    </Suspense>
  );
}

export function SectorTreemap(props: SectorTreemapProps) {
  return (
    <Suspense fallback={<Placeholder className="h-80 sm:h-[400px]" />}>
      <TreemapImpl {...props} />
    </Suspense>
  );
}
