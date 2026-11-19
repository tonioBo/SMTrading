import { describe, expect, it } from "vitest";
import { barTimeOf, chartMoney, fillMarkers, hasVolume, positionLines, protectionLines, sma, toCandles, tradeChartWindow, type OrderLike } from "@/lib/priceChart";

const H = 3600;
const T0 = Date.parse("2026-11-10T14:00:00Z") / 1000;
const bar = (i: number, close: number, extra: Partial<Record<"open" | "high" | "low" | "volume", string>> = {}) => ({
  ts: new Date((T0 + i * H) * 1000).toISOString(),
  close: String(close),
  ...extra,
});

describe("toCandles", () => {
  it("maps OHLCV, sorted, one per second, high/low covering open and close", () => {
    const c = toCandles([bar(1, 11, { open: "10", high: "12", low: "9", volume: "500" }), bar(0, 10, { open: "9", high: "9.5", low: "8" }), bar(1, 11.5, { open: "10" })]);
    expect(c).toEqual([
      { time: T0, open: 9, high: 10, low: 8, close: 10, volume: 0 },
      { time: T0 + H, open: 10, high: 11.5, low: 10, close: 11.5, volume: 0 },
    ]);
  });
  it("an old answer with the close only draws flat candles; bad bars are dropped", () => {
    expect(toCandles([bar(0, 5), { ts: "x", close: "1" }, { ts: bar(1, 0).ts, close: "n/a" }])).toEqual([{ time: T0, open: 5, high: 5, low: 5, close: 5, volume: 0 }]);
  });
  it("volume only when there is some", () => {
    expect(hasVolume(toCandles([bar(0, 1)]))).toBe(false);
    expect(hasVolume(toCandles([bar(0, 1, { volume: "3" })]))).toBe(true);
  });
});

describe("sma", () => {
  it("averages the last n closes from the n-th bar", () => {
    const c = toCandles([1, 2, 3, 4, 5].map((v, i) => bar(i, v)));
    expect(sma(c, 3)).toEqual([
      { time: T0 + 2 * H, value: 2 },
      { time: T0 + 3 * H, value: 3 },
      { time: T0 + 4 * H, value: 4 },
    ]);
    expect(sma(c, 6)).toEqual([]);
  });
});

describe("fills as markers", () => {
  const candles = toCandles([0, 1, 2, 3].map((i) => bar(i, 100 + i)));
  it("puts each fill on the bar it falls in", () => {
    expect(barTimeOf([10, 20, 30], 25, 100)).toBe(20);
    expect(barTimeOf([10, 20, 30], 5, 100)).toBeNull();
    expect(barTimeOf([10, 20, 30], 300, 100)).toBeNull();
  });
  it("▲ buys and ▼ sells, merged per bar and side, off-chart fills left out", () => {
    const at = (s: number) => new Date(s * 1000).toISOString();
    const m = fillMarkers(
      [
        { ts: at(T0 + 2 * H + 60), side: "sell" },
        { ts: at(T0 + 10), side: "buy", text: "Ingresso" },
        { ts: at(T0 + 20), side: "buy", text: "Ingresso" },
        { ts: at(T0 - 3 * H), side: "buy" },
      ],
      candles,
      "1h",
    );
    expect(m).toEqual([
      { time: T0, side: "buy", text: "Ingresso ×2" },
      { time: T0 + 2 * H, side: "sell", text: "Vendita" },
    ]);
  });
});

const order = (o: Partial<OrderLike>): OrderLike => ({ role: null, type: "market", side: "sell", limit_price: null, stop_price: null, ...o });
const usd = (v: number) => chartMoney(v, "USD");

describe("stop and target lines", () => {
  it("the newest stop and target of the orders; entries and rejected orders draw nothing", () => {
    const lines = protectionLines(
      [
        order({ role: "entry", type: "limit", side: "buy", limit_price: "200" }),
        order({ role: "stop_loss", type: "stop", stop_price: "150", created_at: "2026-11-10T15:00:00Z" }),
        order({ role: "stop_loss", type: "stop", stop_price: "160", created_at: "2026-11-11T15:00:00Z" }),
        order({ role: "take_profit", type: "limit", limit_price: "250" }),
        order({ role: "take_profit", type: "limit", limit_price: "999", status: "rejected" }),
      ],
      usd,
    );
    expect(lines).toEqual([
      { price: 160, kind: "stop", title: "Stop 160,00 $" },
      { price: 250, kind: "target", title: "Target 250,00 $" },
    ]);
  });
  it("orders without a role (an OCO «PROTEGGI») by their type", () => {
    const lines = protectionLines([order({ type: "limit", limit_price: "460" }), order({ type: "stop", stop_price: "360" })], usd);
    expect(lines.map((l) => l.kind)).toEqual(["stop", "target"]);
  });
  it("the sheet: average price, stop and target per portfolio, named with more than one", () => {
    const one = positionLines([{ portfolio_name: "Prudente", avg_price: "180" }], [{ ...order({ type: "stop", stop_price: "150" }), portfolio_name: "Prudente" }], usd);
    expect(one.map((l) => l.title)).toEqual(["Prezzo medio", "Stop 150,00 $"]);
    const two = positionLines(
      [
        { portfolio_name: "Prudente", avg_price: "180" },
        { portfolio_name: "Aggressivo", avg_price: "0.5" },
      ],
      [],
      usd,
    );
    expect(two.map((l) => l.title)).toEqual(["Prezzo medio · Prudente", "Prezzo medio · Aggressivo"]);
    expect(chartMoney(0.5, "USD")).toBe("0,5000 USD");
  });
});

describe("tradeChartWindow", () => {
  const now = Date.parse("2026-11-19T12:00:00Z");
  it("hourly bars under 3 days with at least a day of margin, never past now", () => {
    const w = tradeChartWindow("2026-11-17T15:00:00Z", "2026-11-18T15:00:00Z", now);
    expect(w.timeframe).toBe("1h");
    expect(w.start.toISOString()).toBe("2026-11-16T15:00:00.000Z");
    expect(w.end.toISOString()).toBe("2026-11-19T12:00:00.000Z");
  });
  it("daily bars from 3 days, a fifth of the length (at least 10 days) on both sides", () => {
    const w = tradeChartWindow("2026-06-01T00:00:00Z", "2026-11-01T00:00:00Z", now);
    expect(w.timeframe).toBe("1d");
    expect(w.start.getTime()).toBe(Date.parse("2026-06-01T00:00:00Z") - (153 * 86_400_000) / 5);
    expect(w.end.getTime()).toBe(now);
  });
  it("an open trade runs to now", () => {
    const w = tradeChartWindow("2026-11-19T09:00:00Z", null, now);
    expect(w.timeframe).toBe("1h");
    expect(w.end.getTime()).toBe(now);
  });
});
