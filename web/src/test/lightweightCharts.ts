/**
 * A stand-in for `lightweight-charts` in jsdom (no canvas): registered for every test in
 * setup.ts. It records what the components ask for (series with their options and data,
 * price lines, markers, panes) so tests can check it, and lets a test move the crosshair.
 * It refuses what the real library refuses: data not strictly ascending by time, a value
 * that is not a finite number, removing a series the chart does not have (also after
 * `remove()`: «Value is undefined»).
 */
type Handler = (param: { time?: number; point?: { x: number; y: number } }) => void;

export interface FakePriceLine {
  options: Record<string, unknown>;
}

export class FakeSeries {
  data: unknown[] = [];
  priceLines: FakePriceLine[] = [];
  constructor(
    public type: string,
    public options: Record<string, unknown>,
    public pane: number,
  ) {}
  setData(data: unknown[]) {
    let prev = -Infinity;
    data.forEach((d, i) => {
      const item = d as { time: number; value?: unknown; open?: unknown };
      if (!(item.time > prev)) throw new Error(`Assertion failed: data must be asc ordered by time, index=${i}, time=${item.time}, prev time=${prev}`);
      prev = item.time;
      if ("value" in item && (typeof item.value !== "number" || !Number.isFinite(item.value))) {
        throw new Error(`Assertion failed: ${this.type} series item data value must be a number, got=${typeof item.value}, value=${String(item.value)}`);
      }
    });
    this.data = data;
  }
  applyOptions(o: Record<string, unknown>) {
    this.options = { ...this.options, ...o };
  }
  createPriceLine(options: Record<string, unknown>) {
    if (typeof options.price !== "number") throw new Error(`Assertion failed: the type of 'price' price line's property must be a number, got '${typeof options.price}'`);
    const line = { options };
    this.priceLines.push(line);
    return line;
  }
  removePriceLine(line: FakePriceLine) {
    this.priceLines = this.priceLines.filter((l) => l !== line);
  }
  priceScale() {
    return { applyOptions: () => {} };
  }
}

export class FakeChart {
  series: FakeSeries[] = [];
  handlers = new Set<Handler>();
  removed = false;
  paneStretch: Record<number, number> = {};
  constructor(
    public container: HTMLElement,
    public options: Record<string, unknown>,
  ) {}
  addSeries(def: { type: string }, options: Record<string, unknown> = {}, pane = 0) {
    const s = new FakeSeries(def.type, options, pane);
    this.series.push(s);
    return s;
  }
  removeSeries(s: FakeSeries) {
    if (!this.series.includes(s)) throw new Error("Value is undefined");
    this.series = this.series.filter((x) => x !== s);
  }
  applyOptions(o: Record<string, unknown>) {
    this.options = { ...this.options, ...o };
  }
  subscribeCrosshairMove(h: Handler) {
    this.handlers.add(h);
  }
  unsubscribeCrosshairMove(h: Handler) {
    this.handlers.delete(h);
  }
  /** test helper: the crosshair at `time` (UTC seconds), or off the chart */
  moveCrosshair(time?: number, x = 10) {
    this.handlers.forEach((h) => h(time == null ? {} : { time, point: { x, y: 10 } }));
  }
  timeScale() {
    return {
      fitContent: () => {},
      getVisibleLogicalRange: () => null,
      setVisibleLogicalRange: () => {},
      setVisibleRange: () => {},
    };
  }
  panes() {
    return [0, 1].map((i) => ({ setStretchFactor: (f: number) => (this.paneStretch[i] = f) }));
  }
  resize() {}
  remove() {
    this.removed = true;
    this.series = [];
  }
}

/** Every chart created (the newest last); tests clear it. */
export const charts: FakeChart[] = [];
export const markers: { series: FakeSeries; markers: unknown[] }[] = [];

export function createChart(container: HTMLElement, options: Record<string, unknown> = {}) {
  const c = new FakeChart(container, options);
  charts.push(c);
  return c;
}

export function createSeriesMarkers(series: FakeSeries, list: unknown[] = []) {
  const entry = { series, markers: list };
  markers.push(entry);
  return {
    setMarkers: (m: unknown[]) => (entry.markers = m),
    detach: () => {},
  };
}

export const AreaSeries = { type: "Area" };
export const LineSeries = { type: "Line" };
export const CandlestickSeries = { type: "Candlestick" };
export const HistogramSeries = { type: "Histogram" };
export const LineStyle = { Solid: 0, Dotted: 1, Dashed: 2, LargeDashed: 3, SparseDotted: 4 };
export const CrosshairMode = { Normal: 0, Magnet: 1, Hidden: 2, MagnetOHLC: 3 };
export const ColorType = { Solid: "solid", VerticalGradient: "gradient" };
export const PriceScaleMode = { Normal: 0, Logarithmic: 1, Percentage: 2, IndexedTo100: 3 };
