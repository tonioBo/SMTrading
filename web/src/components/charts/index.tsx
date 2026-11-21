/**
 * The charts, loaded on first use (their libraries stay out of the first page load):
 * TradingView Lightweight Charts™ for prices and equity, Apache ECharts for the treemap.
 * While a module loads, a placeholder of the chart's size keeps the layout still.
 */
import { Component, lazy, Suspense, type ReactNode } from "react";
import type { EquityChartProps, OverviewChartProps, PriceChartProps, SectorTreemapProps } from "@/components/charts/types";

const EquityImpl = lazy(() => import("@/components/charts/EquityChart"));
const PriceImpl = lazy(() => import("@/components/charts/PriceChart"));
const TreemapImpl = lazy(() => import("@/components/charts/SectorTreemap"));
const OverviewImpl = lazy(() => import("@/components/charts/OverviewChart"));

function Placeholder({ className }: { className: string }) {
  return <div className={`w-full animate-pulse rounded-md bg-card ${className}`} aria-busy="true" aria-label="Caricamento del grafico" />;
}

/**
 * A chart that throws (data the library refuses, a failed lazy load) shows «Grafico non
 * disponibile» in its place instead of taking the whole page to the app's error screen;
 * a new `resetKey` (another range, unit, symbol) tries again.
 */
export class ChartBoundary extends Component<{ resetKey?: string; className: string; children: ReactNode }, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() {
    return { failed: true };
  }
  componentDidCatch(error: unknown) {
    console.error("Grafico non disponibile", error);
  }
  componentDidUpdate(prev: { resetKey?: string }) {
    if (this.state.failed && prev.resetKey !== this.props.resetKey) this.setState({ failed: false });
  }
  render() {
    if (!this.state.failed) return this.props.children;
    return (
      <div className={`flex w-full items-center justify-center text-sm text-muted ${this.props.className}`} role="status">
        Grafico non disponibile
      </div>
    );
  }
}

export function EquityChart(props: EquityChartProps) {
  return (
    <ChartBoundary resetKey={props.resetKey} className="h-60 sm:h-[300px]">
      <Suspense fallback={<Placeholder className="h-60 sm:h-[300px]" />}>
        <EquityImpl {...props} />
      </Suspense>
    </ChartBoundary>
  );
}

export function PriceChart(props: PriceChartProps) {
  return (
    <ChartBoundary className="h-[366px] sm:h-[446px]">
      <Suspense fallback={<Placeholder className="h-[366px] sm:h-[446px]" />}>
        <PriceImpl {...props} />
      </Suspense>
    </ChartBoundary>
  );
}

export function SectorTreemap(props: SectorTreemapProps) {
  return (
    <Suspense fallback={<Placeholder className="h-80 sm:h-[400px]" />}>
      <TreemapImpl {...props} />
    </Suspense>
  );
}

export function OverviewChart(props: OverviewChartProps) {
  return (
    <ChartBoundary resetKey={props.ariaLabel} className="h-[220px] sm:h-[280px]">
      <Suspense fallback={<Placeholder className="h-[220px] sm:h-[280px]" />}>
        <OverviewImpl {...props} />
      </Suspense>
    </ChartBoundary>
  );
}
