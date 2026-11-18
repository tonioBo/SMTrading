import { useMemo, useState, type PointerEvent } from "react";
import { nearestIndex, niceTicks, timeLabel, type ChartPoint } from "@/lib/lineChart";

export interface ChartLine {
  key: string;
  label: string;
  color: string;
  points: ChartPoint[];
  /** reference lines (the goals): dashed, left out of the tooltip */
  reference?: boolean;
  /** a short label drawn at the right end of the line */
  tag?: string;
  /** a thinner solid stroke (the indices compared by the goals) */
  thin?: boolean;
  /** a thicker stroke (the «Totale» of the portfolios) */
  bold?: boolean;
}

interface Props {
  lines: ChartLine[];
  /** tooltip values */
  formatValue: (v: number) => string;
  /** y axis labels (`step` = the distance between two ticks) */
  formatTick: (v: number, step: number) => string;
  /** x axis labels: hours for one day, dates otherwise */
  axisTime: "time" | "day";
  ariaLabel: string;
}

const W = 1000; // the SVG's own units: stretched to the box (non-scaling strokes)
const H = 1000;
const X_TICKS = 5;

/**
 * Hand-made SVG line chart (no chart library): several series on one time axis, y grid
 * with round ticks, a hover/touch guide with a tooltip listing every series' nearest
 * value. Axis labels are HTML, positioned in percent, so they never stretch.
 */
export function LineChart({ lines, formatValue, formatTick, axisTime, ariaLabel }: Props) {
  const [hoverT, setHoverT] = useState<number | null>(null);
  const geo = useMemo(() => {
    const all = lines.flatMap((l) => l.points);
    let t0 = Math.min(...all.map((p) => p.t));
    let t1 = Math.max(...all.map((p) => p.t));
    if (t0 === t1) {
      t0 -= 30 * 60_000;
      t1 += 30 * 60_000;
    }
    // the scale follows the real series; reference lines (goals) only when there is none
    const scaled = lines.filter((l) => !l.reference).flatMap((l) => l.points);
    const vs = (scaled.length > 0 ? scaled : all).map((p) => p.v);
    const ticks = niceTicks(Math.min(...vs), Math.max(...vs), 4);
    const v0 = ticks[0];
    const v1 = ticks[ticks.length - 1];
    const x = (t: number) => ((t - t0) / (t1 - t0)) * W;
    const y = (v: number) => H - ((v - v0) / (v1 - v0)) * H;
    const xTicks = Array.from({ length: X_TICKS }, (_, i) => t0 + ((t1 - t0) * (i + 0.5)) / X_TICKS);
    /** a reference line outside the scale: drawn as a label at the top or bottom edge */
    const outside = (l: ChartLine): "up" | "down" | null => {
      if (!l.reference || l.points.length === 0) return null;
      if (l.points.every((p) => p.v > v1)) return "up";
      if (l.points.every((p) => p.v < v0)) return "down";
      return null;
    };
    return { t0, t1, ticks, step: ticks.length > 1 ? ticks[1] - ticks[0] : 1, x, y, xTicks, outside };
  }, [lines]);

  const data = lines.filter((l) => !l.reference && l.points.length > 0);
  const hover = useMemo(() => {
    if (hoverT == null || data.length === 0) return null;
    const hits = data.map((l) => ({ line: l, point: l.points[nearestIndex(l.points, hoverT)] }));
    // the guide sits on the hit closest to the pointer
    const t = hits.reduce((best, h) => (Math.abs(h.point.t - hoverT) < Math.abs(best - hoverT) ? h.point.t : best), hits[0].point.t);
    return { t, hits };
  }, [hoverT, data]);

  function onMove(e: PointerEvent<HTMLDivElement>) {
    const rect = e.currentTarget.getBoundingClientRect();
    const frac = rect.width > 0 ? Math.min(1, Math.max(0, (e.clientX - rect.left) / rect.width)) : 1;
    setHoverT(geo.t0 + frac * (geo.t1 - geo.t0));
  }

  const pct = (n: number, of: number) => `${(n / of) * 100}%`;
  const right = hover ? geo.x(hover.t) > W / 2 : false;

  return (
    <div className="relative h-64 w-full select-none text-xs text-muted" role="img" aria-label={ariaLabel} data-testid="line-chart">
      {/* y labels */}
      <div className="absolute bottom-6 left-0 top-2 w-14">
        {geo.ticks.map((v) => (
          <span key={v} className="num absolute right-2 -translate-y-1/2" style={{ top: pct(geo.y(v), H) }}>
            {formatTick(v, geo.step)}
          </span>
        ))}
      </div>
      {/* x labels */}
      <div className="absolute bottom-0 left-14 right-2 h-5">
        {geo.xTicks.map((t) => (
          <span key={t} className="num absolute -translate-x-1/2 whitespace-nowrap" style={{ left: pct(geo.x(t), W) }}>
            {timeLabel(t, axisTime)}
          </span>
        ))}
      </div>
      <div
        className="absolute bottom-6 left-14 right-2 top-2 touch-pan-y"
        onPointerMove={onMove}
        onPointerDown={onMove}
        onPointerLeave={() => setHoverT(null)}
        data-testid="line-chart-plot"
      >
        <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" className="h-full w-full overflow-visible" aria-hidden="true">
          {geo.ticks.map((v) => (
            <line key={v} x1={0} x2={W} y1={geo.y(v)} y2={geo.y(v)} stroke="rgb(var(--border))" strokeWidth={1} vectorEffect="non-scaling-stroke" />
          ))}
          {lines.map((l) =>
            l.points.length > 1 && !geo.outside(l) ? (
              <path
                key={l.key}
                d={l.points.map((p, i) => `${i ? "L" : "M"}${geo.x(p.t).toFixed(2)},${geo.y(p.v).toFixed(2)}`).join("")}
                fill="none"
                stroke={l.color}
                strokeWidth={l.thin ? 1 : l.reference ? 1.25 : l.bold ? 3 : 2}
                strokeDasharray={l.reference ? "6 4" : undefined}
                strokeLinejoin="round"
                vectorEffect="non-scaling-stroke"
                data-testid={`line-${l.key}`}
              />
            ) : null,
          )}
          {hover && (
            <line x1={geo.x(hover.t)} x2={geo.x(hover.t)} y1={0} y2={H} stroke="rgb(var(--muted))" strokeWidth={1} vectorEffect="non-scaling-stroke" />
          )}
        </svg>
        {/* single points and hover markers: HTML dots stay round */}
        {data
          .filter((l) => l.points.length === 1)
          .map((l) => (
            <Dot key={l.key} color={l.color} left={pct(geo.x(l.points[0].t), W)} top={pct(geo.y(l.points[0].v), H)} />
          ))}
        {lines
          .filter((l) => geo.outside(l))
          .map((l) => {
            const up = geo.outside(l) === "up";
            return (
              <span
                key={`edge-${l.key}`}
                className={`pointer-events-none absolute right-0 whitespace-nowrap text-[10px] font-medium ${up ? "top-0" : "bottom-0"}`}
                style={{ color: l.color }}
                data-testid={`edge-${l.key}`}
              >
                {`${l.tag ?? l.label} ${up ? "↑" : "↓"}`}
              </span>
            );
          })}
        {lines
          .filter((l) => l.tag && l.points.length > 0 && !geo.outside(l))
          .map((l) => {
            const last = l.points[l.points.length - 1];
            return (
              <span
                key={`tag-${l.key}`}
                className="pointer-events-none absolute -translate-x-full -translate-y-full whitespace-nowrap pb-0.5 text-[10px] font-medium"
                style={{ left: pct(geo.x(last.t), W), top: pct(geo.y(last.v), H), color: l.color }}
                data-testid={`tag-${l.key}`}
              >
                {l.tag}
              </span>
            );
          })}
        {hover?.hits.map((h) => (
          <Dot key={h.line.key} color={h.line.color} left={pct(geo.x(h.point.t), W)} top={pct(geo.y(h.point.v), H)} ring />
        ))}
        {hover && (
          <div
            role="tooltip"
            className="pointer-events-none absolute top-0 z-10 min-w-40 rounded-md border border-border bg-bg/95 p-2 text-fg shadow-md"
            style={right ? { right: `calc(${pct(W - geo.x(hover.t), W)} + 8px)` } : { left: `calc(${pct(geo.x(hover.t), W)} + 8px)` }}
          >
            <p className="num mb-1 text-muted">{timeLabel(hover.t, "full")}</p>
            <ul className="space-y-0.5">
              {hover.hits.map((h) => (
                <li key={h.line.key} className="flex items-center justify-between gap-3">
                  <span className="flex items-center gap-1.5">
                    <span className="inline-block h-2 w-2 rounded-full" style={{ background: h.line.color }} />
                    {h.line.label}
                  </span>
                  <span className="num font-medium">{formatValue(h.point.v)}</span>
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>
    </div>
  );
}

function Dot({ color, left, top, ring }: { color: string; left: string; top: string; ring?: boolean }) {
  return (
    <span
      className={`pointer-events-none absolute h-2 w-2 -translate-x-1/2 -translate-y-1/2 rounded-full ${ring ? "ring-2 ring-bg" : ""}`}
      style={{ left, top, background: color }}
    />
  );
}
