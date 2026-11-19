/**
 * Candlesticks (Lightweight Charts) with a volume pane, moving-average overlays, our stop /
 * target / entry as labelled price lines (kept in the autoscale) and our executions as
 * markers (▲ buy, ▼ sell); a legend with the hovered bar's OHLC. Lazily loaded
 * (components/charts/index.tsx).
 */
import {
  CandlestickSeries,
  createSeriesMarkers,
  HistogramSeries,
  LineSeries,
  LineStyle,
  type AutoscaleInfo,
  type ISeriesApi,
  type MouseEventParams,
  type SeriesType,
  type Time,
  type UTCTimestamp,
} from "lightweight-charts";
import { useEffect, useMemo, useRef, useState } from "react";
import { usePalette } from "@/components/charts/hooks";
import { customFormat, useLwChart } from "@/components/charts/lwc";
import type { PriceChartProps } from "@/components/charts/types";
import { crosshairLabel, priceLabel, volumeLabel, withAlpha, type Palette } from "@/lib/chartTheme";
import { hasVolume, type PriceLineKind } from "@/lib/priceChart";
import { useIsNarrow } from "@/lib/media";

function lineColor(kind: PriceLineKind, p: Palette): string {
  return kind === "stop" ? p.loss : kind === "target" ? p.gain : kind === "entry" ? p.accent : p.warn;
}

export default function PriceChart({ candles, timeframe, volume = true, overlays = [], lines = [], markers = [], ariaLabel, formatPrice = priceLabel }: PriceChartProps) {
  const ref = useRef<HTMLDivElement>(null);
  const palette = usePalette();
  const narrow = useIsNarrow();
  const withVolume = volume && hasVolume(candles);
  const height = (narrow ? 280 : 340) + (withVolume ? (narrow ? 70 : 90) : 0);
  const daily = timeframe === "1d";
  const chart = useLwChart(ref, { palette, daily, height });
  const [hover, setHover] = useState<number | null>(null);
  const byTime = useMemo(() => new Map(candles.map((c) => [c.time, c])), [candles]);

  useEffect(() => {
    if (!chart) return;
    const created: ISeriesApi<SeriesType>[] = [];
    const levels = lines.map((l) => l.price);
    const main = chart.addSeries(CandlestickSeries, {
      upColor: palette.gain,
      downColor: palette.loss,
      wickUpColor: palette.gain,
      wickDownColor: palette.loss,
      borderVisible: false,
      priceFormat: customFormat(formatPrice),
      priceLineVisible: false,
      // our stop and target stay in view even when far from the bars
      autoscaleInfoProvider: (original: () => AutoscaleInfo | null) => {
        const info = original();
        if (!info?.priceRange || levels.length === 0) return info;
        return {
          ...info,
          priceRange: {
            minValue: Math.min(info.priceRange.minValue, ...levels),
            maxValue: Math.max(info.priceRange.maxValue, ...levels),
          },
        };
      },
    });
    main.setData(candles.map((c) => ({ time: c.time as UTCTimestamp, open: c.open, high: c.high, low: c.low, close: c.close })));
    created.push(main);
    for (const l of lines) {
      const color = lineColor(l.kind, palette);
      main.createPriceLine({ price: l.price, color, lineWidth: 1, lineStyle: l.kind === "entry" ? LineStyle.Dotted : LineStyle.Dashed, axisLabelVisible: true, title: l.title });
    }
    const marks = createSeriesMarkers(
      main,
      markers.map((m) => ({
        time: m.time as UTCTimestamp,
        position: m.side === "buy" ? ("belowBar" as const) : ("aboveBar" as const),
        shape: m.side === "buy" ? ("arrowUp" as const) : ("arrowDown" as const),
        color: m.side === "buy" ? palette.gain : palette.loss,
        text: m.text,
      })),
    );
    for (const o of overlays) {
      const s = chart.addSeries(LineSeries, { color: o.color, lineWidth: 1, priceFormat: customFormat(formatPrice), priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false, title: o.label });
      s.setData(o.data.map((p) => ({ time: p.time as UTCTimestamp, value: p.value })));
      created.push(s);
    }
    if (withVolume) {
      const v = chart.addSeries(
        HistogramSeries,
        { priceFormat: customFormat(volumeLabel, 1), priceLineVisible: false, lastValueVisible: false },
        1,
      );
      v.setData(
        candles.map((c) => ({ time: c.time as UTCTimestamp, value: c.volume, color: withAlpha(c.close >= c.open ? palette.gain : palette.loss, 0.45) })),
      );
      created.push(v);
      // the candles keep most of the height: relative sizes (a fixed height is lost on a resize)
      const [prices, volumes] = chart.panes();
      prices?.setStretchFactor(narrow ? 280 : 340);
      volumes?.setStretchFactor(narrow ? 70 : 90);
    }
    chart.timeScale().fitContent();
    return () => {
      marks.detach();
      created.forEach((s) => chart.removeSeries(s));
    };
  }, [chart, candles, overlays, lines, markers, palette, withVolume, narrow, formatPrice]);

  useEffect(() => {
    if (!chart) return;
    const onMove = (param: MouseEventParams<Time>) => setHover(param.time == null ? null : Number(param.time));
    chart.subscribeCrosshairMove(onMove);
    return () => chart.unsubscribeCrosshairMove(onMove);
  }, [chart]);

  const bar = (hover != null ? byTime.get(hover) : undefined) ?? candles[candles.length - 1];
  return (
    <div className="relative w-full select-none" role="img" aria-label={ariaLabel} data-testid="price-chart">
      {bar && (
        <p className="num pointer-events-none absolute left-1 top-0 z-10 flex flex-wrap gap-x-2 text-[11px] text-muted" data-testid="price-legend">
          <span>{crosshairLabel(bar.time, daily)}</span>
          <span>Ap. {formatPrice(bar.open)}</span>
          <span>Max {formatPrice(bar.high)}</span>
          <span>Min {formatPrice(bar.low)}</span>
          <span className={bar.close >= bar.open ? "text-gain" : "text-loss"}>Chius. {formatPrice(bar.close)}</span>
          {withVolume && <span>Vol {volumeLabel(bar.volume)}</span>}
        </p>
      )}
      <div ref={ref} className="w-full pt-4" style={{ height: height + 16 }} />
    </div>
  );
}
