/**
 * TradingView Lightweight Charts™ (Apache-2.0, https://www.tradingview.com/): one chart
 * per container, created on mount and removed on unmount, sized by a ResizeObserver,
 * themed from the CSS tokens, it-IT numbers and Europe/Rome times. The TradingView
 * attribution logo stays on (the licence's link requirement; public/NOTICE.txt carries the
 * notice). Touch: a horizontal drag pans, a pinch zooms, a vertical swipe scrolls the page.
 * Imported only by the lazily loaded chart components.
 */
import { createChart, ColorType, CrosshairMode, LineStyle, type DeepPartial, type ChartOptions, type IChartApi, type ISeriesApi, type SeriesType, type Time } from "lightweight-charts";
import { useEffect, useRef, useState, type RefObject } from "react";
import { crosshairLabel, tickLabel, withAlpha, type Palette } from "@/lib/chartTheme";
import { useResize } from "@/components/charts/hooks";

/** The options that follow the theme (applied again when it changes). */
export function themeOptions(p: Palette): DeepPartial<ChartOptions> {
  return {
    layout: {
      background: { type: ColorType.Solid, color: "transparent" },
      textColor: p.muted,
      fontFamily: "Inter, system-ui, sans-serif",
      fontSize: 11,
      attributionLogo: false,
      panes: { separatorColor: p.border, separatorHoverColor: withAlpha(p.muted, 0.3) },
    },
    grid: { vertLines: { color: withAlpha(p.border, 0.6) }, horzLines: { color: withAlpha(p.border, 0.9) } },
    rightPriceScale: { borderColor: p.border },
    timeScale: { borderColor: p.border },
    crosshair: {
      vertLine: { color: p.muted, labelBackgroundColor: p.card, style: LineStyle.Dashed },
      horzLine: { color: p.muted, labelBackgroundColor: p.card, style: LineStyle.Dashed },
    },
  };
}

/** A series' price labels (axis, last value, price lines, crosshair): set on each series, so
 * a volume pane keeps its own. */
export function customFormat(formatter: (v: number) => string, minMove = 0.000001) {
  return { type: "custom" as const, formatter, minMove };
}

function baseOptions(p: Palette, daily: boolean, height: number): DeepPartial<ChartOptions> {
  const theme = themeOptions(p);
  return {
    ...theme,
    height,
    autoSize: false,
    crosshair: { ...theme.crosshair, mode: CrosshairMode.Magnet },
    localization: {
      locale: "it-IT",
      timeFormatter: (t: Time) => crosshairLabel(Number(t), daily),
    },
    timeScale: {
      ...theme.timeScale,
      timeVisible: !daily,
      secondsVisible: false,
      rightOffset: 2,
      tickMarkFormatter: (t: Time, kind: number) => tickLabel(Number(t), kind),
    },
    rightPriceScale: { ...theme.rightPriceScale, scaleMargins: { top: 0.1, bottom: 0.08 } },
    handleScroll: { mouseWheel: true, pressedMouseMove: true, horzTouchDrag: true, vertTouchDrag: false },
    handleScale: { mouseWheel: true, pinch: true, axisPressedMouseMove: true, axisDoubleClickReset: true },
  };
}

/** The charts already removed (unmount): their series are gone with them. */
const removedCharts = new WeakSet<IChartApi>();

/** Whether `chart` is still alive (not removed on unmount). */
export const isChartAlive = (chart: IChartApi) => !removedCharts.has(chart);

/**
 * Removes `series` from `chart` in an effect's cleanup. On unmount React runs the chart's
 * own cleanup (`chart.remove()`, declared first) before the series' cleanups, and
 * `removeSeries` on a removed chart throws «Value is undefined»: a removed chart is skipped.
 */
export function removeSeriesSafely(chart: IChartApi, series: readonly ISeriesApi<SeriesType>[]): void {
  if (!isChartAlive(chart)) return;
  for (const s of series) {
    try {
      chart.removeSeries(s);
    } catch {
      // already gone with its chart
    }
  }
}

/**
 * A Lightweight Chart in `ref` (null until mounted), its options applied again when `daily`,
 * the height or the palette (the theme) change.
 */
export function useLwChart(
  ref: RefObject<HTMLDivElement>,
  { palette, daily, height }: { palette: Palette; daily: boolean; height: number },
): IChartApi | null {
  const [chart, setChart] = useState<IChartApi | null>(null);
  const initial = useRef({ palette, daily, height });
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const o = initial.current;
    const c = createChart(el, { ...baseOptions(o.palette, o.daily, o.height), width: el.clientWidth || 320 });
    setChart(c);
    return () => {
      setChart(null);
      removedCharts.add(c);
      c.remove();
    };
  }, [ref]);
  useResize(ref, (width) => chart?.applyOptions({ width }), chart);
  useEffect(() => {
    chart?.applyOptions(baseOptions(palette, daily, height));
  }, [chart, palette, daily, height]);
  return chart;
}
