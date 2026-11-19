/**
 * «Andamento» (Lightweight Charts): a gradient area per portfolio, the «Totale» bold, the
 * indices thin, the goals as labelled dashed price lines; a crosshair tooltip listing every
 * series with its value and change. Lazily loaded (components/charts/index.tsx).
 */
import { AreaSeries, LineSeries, LineStyle, type ISeriesApi, type LogicalRange, type MouseEventParams, type SeriesType, type Time, type UTCTimestamp } from "lightweight-charts";
import { useEffect, useMemo, useRef, useState } from "react";
import { usePalette } from "@/components/charts/hooks";
import { customFormat, useLwChart } from "@/components/charts/lwc";
import type { EquityChartProps } from "@/components/charts/types";
import { equitySeries } from "@/lib/equityHistory";
import { timeLabel } from "@/lib/lineChart";
import { useIsNarrow } from "@/lib/media";

export default function EquityChart({ lines, single, formatValue, rowsAt, daily, ariaLabel, resetKey }: EquityChartProps) {
  const ref = useRef<HTMLDivElement>(null);
  const palette = usePalette();
  const height = useIsNarrow() ? 240 : 300;
  const chart = useLwChart(ref, { palette, daily, height });
  const [tip, setTip] = useState<{ t: number; x: number } | null>(null);
  const zoom = useRef<{ key: string; range: LogicalRange | null } | null>(null);
  const specs = useMemo(() => equitySeries(lines, palette, single), [lines, palette, single]);

  useEffect(() => {
    if (!chart) return;
    const created: ISeriesApi<SeriesType>[] = [];
    const priceFormat = customFormat(formatValue, 0.01);
    for (const s of specs) {
      const data = s.data.map((p) => ({ time: p.time as UTCTimestamp, value: p.value }));
      if (s.kind === "area") {
        const a = chart.addSeries(AreaSeries, {
          lineColor: s.color,
          topColor: s.topColor,
          bottomColor: s.bottomColor,
          lineWidth: s.lineWidth,
          priceFormat,
          priceLineVisible: false,
          lastValueVisible: single,
        });
        a.setData(data);
        created.push(a);
      } else if (s.kind === "line") {
        const l = chart.addSeries(LineSeries, {
          color: s.color,
          lineWidth: s.lineWidth,
          priceFormat,
          priceLineVisible: false,
          lastValueVisible: s.lineWidth === 3,
          crosshairMarkerVisible: s.lineWidth === 3,
        });
        l.setData(data);
        created.push(l);
      } else {
        // the goal: an invisible flat series (it keeps the target in the autoscale) and its line
        const g = chart.addSeries(LineSeries, {
          color: s.color,
          priceFormat,
          lineVisible: false,
          pointMarkersVisible: false,
          priceLineVisible: false,
          lastValueVisible: false,
          crosshairMarkerVisible: false,
        });
        g.setData(data);
        if (s.priceLine) {
          g.createPriceLine({ price: s.priceLine.price, color: s.priceLine.color, lineWidth: 1, lineStyle: LineStyle.Dashed, axisLabelVisible: true, title: s.priceLine.title });
        }
        created.push(g);
      }
    }
    const ts = chart.timeScale();
    const kept = zoom.current?.key === resetKey ? zoom.current.range : null;
    if (kept) ts.setVisibleLogicalRange(kept);
    else ts.fitContent();
    return () => {
      zoom.current = { key: resetKey, range: ts.getVisibleLogicalRange() };
      created.forEach((s) => chart.removeSeries(s));
    };
  }, [chart, specs, single, resetKey, formatValue]);

  useEffect(() => {
    if (!chart) return;
    const onMove = (param: MouseEventParams<Time>) => {
      if (param.time == null || !param.point || param.point.x < 0) setTip(null);
      else setTip({ t: Number(param.time) * 1000, x: param.point.x });
    };
    chart.subscribeCrosshairMove(onMove);
    return () => chart.unsubscribeCrosshairMove(onMove);
  }, [chart]);

  const rows = tip ? rowsAt(tip.t) : [];
  const width = ref.current?.clientWidth ?? 0;
  const right = tip != null && width > 0 && tip.x > width / 2;
  return (
    <div className="relative w-full select-none" role="img" aria-label={ariaLabel} data-testid="equity-chart">
      <div ref={ref} className="w-full" style={{ height }} />
      {tip && rows.length > 0 && (
        <div
          role="tooltip"
          className="pointer-events-none absolute top-1 z-10 min-w-44 max-w-[85%] rounded-md border border-border bg-bg/95 p-2 text-xs text-fg shadow-md"
          style={right ? { right: width - tip.x + 12 } : { left: tip.x + 12 }}
        >
          <p className="num mb-1 text-muted">{timeLabel(tip.t, "full")}</p>
          <ul className="space-y-0.5">
            {rows.map((r) => (
              <li key={r.key} className={`flex items-baseline justify-between gap-3 ${r.bold ? "font-semibold" : ""}`}>
                <span className="flex min-w-0 items-center gap-1.5">
                  <span className="inline-block h-2 w-2 shrink-0 rounded-full" style={{ background: r.color }} />
                  <span className="truncate">{r.label}</span>
                </span>
                <span className="num whitespace-nowrap text-right">
                  <span className="font-medium">{r.value}</span>
                  {r.change && <span className="block text-[10px] text-muted">{r.change}</span>}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
