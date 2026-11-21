import { describe, expect, it } from "vitest";
import { bigNumber, byDay, countdown, dayLabel, marketLine, overviewValue, sparkPoints } from "@/lib/marketsV2";

const NOW = new Date("2026-11-21T12:00:00Z");

describe("marketsV2 helpers", () => {
  it("counts down", () => {
    expect(countdown("2026-11-21T12:00:30Z", NOW)).toBe("meno di 1 min");
    expect(countdown("2026-11-21T12:12:00Z", NOW)).toBe("12 min");
    expect(countdown("2026-11-21T14:05:00Z", NOW)).toBe("2 h 05 min");
    expect(countdown("2026-11-23T14:30:00Z", NOW)).toBe("2 g 2 h");
    expect(countdown("2026-11-23T12:10:00Z", NOW)).toBe("2 g");
  });

  it("describes the market", () => {
    expect(marketLine({ is_open: false, next_open: "2026-11-23T14:30:00Z", next_close: null, market: "NYSE" }, NOW)).toBe("Chiuso · apre tra 2 g 2 h");
    expect(marketLine({ is_open: true, next_open: null, next_close: "2026-11-21T13:00:00Z", market: "NYSE" }, NOW)).toBe("Aperto · chiude tra 1 h 00 min");
    expect(marketLine({ is_open: true, next_open: null, next_close: null, market: "CRYPTO" }, NOW)).toBe("Aperto");
    expect(marketLine({ is_open: null, next_open: null, next_close: null, market: "X" }, NOW)).toBe("");
  });

  it("draws a sparkline", () => {
    expect(sparkPoints([[1, 1]], 100, 20)).toBe("");
    expect(sparkPoints([[0, 1], [10, 3], [20, 2]], 100, 20)).toBe("0.0,18.0 50.0,2.0 100.0,10.0");
    expect(sparkPoints([[0, 5], [10, 5]], 100, 20)).toBe("0.0,10.0 100.0,10.0");
  });

  it("formats values", () => {
    expect(overviewValue({ value: "6910.39", decimals: 2, unit: "" })).toBe("6.910,39");
    expect(overviewValue({ value: "4.9119997", decimals: 3, unit: "%" })).toBe("4,912%");
    expect(overviewValue({ value: null, decimals: 2, unit: "" })).toBe("—");
    expect(bigNumber("4978144444416")).toBe("4,98 T");
    expect(bigNumber("68571832320")).toBe("68,57 Mld");
    expect(bigNumber("512300000")).toBe("512,30 Mln");
    expect(bigNumber(null)).toBe("—");
  });

  it("groups the calendar by day", () => {
    expect(dayLabel("2026-11-24")).toBe("mar 24/11");
    const e = (date: string, symbol: string) => ({ date, kind: "earnings" as const, symbol, name: symbol, sector: null, held: false, portfolios: [], estimate: false, label: null });
    const days = byDay([e("2026-11-25", "B"), e("2026-11-24", "A"), e("2026-11-25", "C")]);
    expect(days.map(([d, es]) => [d, es.map((x) => x.symbol)])).toEqual([
      ["2026-11-24", ["A"]],
      ["2026-11-25", ["B", "C"]],
    ]);
  });
});
