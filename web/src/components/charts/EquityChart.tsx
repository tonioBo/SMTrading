/**
 * «Andamento» (Lightweight Charts): a gradient area per portfolio, the «Totale» bold, the
 * indices thin, the goals as labelled dashed price lines; a crosshair tooltip listing every
 * series with its value and change. Lazily loaded (components/charts/index.tsx).
 */
import { AreaSeries, LineSeries, LineStyle, type ISeriesApi, type LogicalRange, type MouseEventParams, type SeriesType, type Time, type UTCTimestamp } from "lightweight-charts";
import { useEffect, useMemo, useRef, useState } from "react";
import { usePalette } from "@/components/charts/hooks";
import { customFormat, isChartAlive, removeSeriesSafely, useLwChart } from "@/components/charts/lwc";
import type { EquityChartProps } from "@/components/charts/types";
import { equitySeries, type EquitySeries } from "@/lib/equityHistory";
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
  /** the specs the library refused (setData threw): «Grafico non disponibile», not a crashed page */
  const [failed, setFailed] = useState<EquitySeries[] | null>(null);
  const broken = failed === specs;

  useEffect(() => {
    if (!chart) return;
    const created: ISeriesApi<SeriesType>[] = [];
    const priceFormat = customFormat(formatValue, 0.01);
    const add = (s: EquitySeries) => {
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
        created.push(a);
        a.setData(data);
      } else if (s.kind === "line") {
        const l = chart.addSeries(LineSeries, {
          color: s.color,
          lineWidth: s.lineWidth,
          priceFormat,
          priceLineVisible: false,
          lastValueVisible: s.lineWidth === 3,
          crosshairMarkerVisible: s.lineWidth === 3,
        });
        created.push(l);
        l.setData(data);
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
        created.push(g);
        g.setData(data);
        if (s.priceLine) {
          g.createPriceLine({ price: s.priceLine.price, color: s.priceLine.color, lineWidth: 1, lineStyle: LineStyle.Dashed, axisLabelVisible: true, title: s.priceLine.title });
        }
      }
    };
    try {
      specs.forEach(add);
    } catch (e) {
      // data the library refuses: this chart says so, the page goes on
      console.error("Andamento: grafico non disponibile", e);
      removeSeriesSafely(chart, created);
      setFailed(specs);
      return;
    }
    const ts = chart.timeScale();
    const kept = zoom.current?.key === resetKey ? zoom.current.range : null;
    if (kept) ts.setVisibleLogicalRange(kept);
    else ts.fitContent();
    return () => {
      // on unmount useLwChart's cleanup (declared first) has already removed the chart and
      // its series: removeSeries on it would throw «Value is undefined» (the «6h» crash)
      if (!isChartAlive(chart)) return;
      zoom.current = { key: resetKey, range: ts.getVisibleLogicalRange() };
      removeSeriesSafely(chart, created);
    };
  }, [chart, specs, single, resetKey, formatValue]);

  useEffect(() => {
    if (!chart) return;
    const onMove = (param: MouseEventParams<Time>) => {
      if (param.time == null || !param.point || param.point.x < 0) setTip(null);
      else setTip({ t: Number(param.time) * 1000, x: param.point.x });
    };
    chart.subscribeCrosshairMove(onMove);
    return () => {
      if (isChartAlive(chart)) chart.unsubscribeCrosshairMove(onMove);
    };
  }, [chart]);

  const rows = tip && !broken ? rowsAt(tip.t) : [];
  const width = ref.current?.clientWidth ?? 0;
  const right = tip != null && width > 0 && tip.x > width / 2;
  return (
    <div className="relative w-full select-none" role="img" aria-label={ariaLabel} data-testid="equity-chart">
      <div ref={ref} className={broken ? "invisible w-full" : "w-full"} style={{ height }} />
      {broken && (
        <p className="absolute inset-0 flex items-center justify-center text-sm text-muted" role="status">
          Grafico non disponibile
        </p>
      )}
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
