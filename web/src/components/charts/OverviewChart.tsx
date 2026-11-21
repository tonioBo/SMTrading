/**
 * The «Mercati oggi» chart (Lightweight Charts): one index's line as a gradient area,
 * green when it ends above where it started, red below. Lazily loaded
 * (components/charts/index.tsx).
 */
import { AreaSeries, type UTCTimestamp } from "lightweight-charts";
import { useEffect, useRef } from "react";
import { usePalette } from "@/components/charts/hooks";
import { customFormat, isChartAlive, removeSeriesSafely, useLwChart } from "@/components/charts/lwc";
import type { OverviewChartProps } from "@/components/charts/types";
import { withAlpha } from "@/lib/chartTheme";
import { useIsNarrow } from "@/lib/media";

export default function OverviewChart({ points, daily, formatValue, ariaLabel }: OverviewChartProps) {
  const ref = useRef<HTMLDivElement>(null);
  const palette = usePalette();
  const height = useIsNarrow() ? 220 : 280;
  const chart = useLwChart(ref, { palette, daily, height });

  useEffect(() => {
    if (!chart || points.length < 2) return;
    const up = points[points.length - 1][1] >= points[0][1];
    const color = up ? palette.gain : palette.loss;
    const s = chart.addSeries(AreaSeries, {
      lineColor: color,
      topColor: withAlpha(color, 0.25),
      bottomColor: withAlpha(color, 0.02),
      lineWidth: 2,
      priceLineVisible: false,
      priceFormat: customFormat(formatValue, 0.0001),
    });
    // a time once only (the library refuses duplicates), in order
    const seen = new Set<number>();
    const data = [...points]
      .sort((a, b) => a[0] - b[0])
      .filter(([t]) => (seen.has(t) ? false : (seen.add(t), true)))
      .map(([t, v]) => ({ time: t as UTCTimestamp, value: v }));
    s.setData(data);
    chart.timeScale().fitContent();
    return () => {
      if (isChartAlive(chart)) removeSeriesSafely(chart, [s]);
    };
  }, [chart, points, palette, formatValue]);

  return <div ref={ref} role="img" aria-label={ariaLabel} className="w-full" style={{ height }} />;
}
