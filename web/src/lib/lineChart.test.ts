import { describe, expect, it } from "vitest";
import { chartNumber, nearestIndex, niceTicks, timeLabel, toPctPoints } from "@/lib/lineChart";

describe("niceTicks", () => {
  it("covers the range with round steps", () => {
    expect(niceTicks(98.3, 104.1, 4)).toEqual([98, 100, 102, 104, 106]);
    const t = niceTicks(-0.8, 2.3, 4);
    expect(t[0]).toBeLessThanOrEqual(-0.8);
    expect(t[t.length - 1]).toBeGreaterThanOrEqual(2.3);
    expect(t).toContain(0);
  });
  it("a flat range still gets ticks around the value", () => {
    const t = niceTicks(250, 250, 4);
    expect(t.length).toBeGreaterThanOrEqual(2);
    expect(t[0]).toBeLessThan(250);
    expect(t[t.length - 1]).toBeGreaterThan(250);
  });
});

describe("toPctPoints", () => {
  it("is the change since the first point", () => {
    expect(toPctPoints([{ t: 1, v: 200 }, { t: 2, v: 204 }, { t: 3, v: 190 }])).toEqual([
      { t: 1, v: 0 },
      { t: 2, v: 2 },
      { t: 3, v: -5 },
    ]);
  });
  it("is empty without a positive first value", () => {
    expect(toPctPoints([{ t: 1, v: 0 }, { t: 2, v: 5 }])).toEqual([]);
    expect(toPctPoints([])).toEqual([]);
  });
});

describe("nearestIndex", () => {
  const pts = [{ t: 10, v: 0 }, { t: 20, v: 0 }, { t: 40, v: 0 }];
  it("finds the closest point in time", () => {
    expect(nearestIndex(pts, 0)).toBe(0);
    expect(nearestIndex(pts, 14)).toBe(0);
    expect(nearestIndex(pts, 16)).toBe(1);
    expect(nearestIndex(pts, 31)).toBe(2);
    expect(nearestIndex(pts, 99)).toBe(2);
    expect(nearestIndex([], 5)).toBe(-1);
  });
});

describe("labels", () => {
  it("formats numbers the Italian way", () => {
    expect(chartNumber(1234.5, 2)).toBe("1.234,50");
    expect(chartNumber(-0.004, 2)).toBe("0,00");
  });
  it("formats times in Europe/Rome", () => {
    const t = Date.parse("2026-11-16T08:05:00Z");
    expect(timeLabel(t, "time")).toBe("09:05");
    expect(timeLabel(t, "day")).toBe("16/11");
    expect(timeLabel(t, "full")).toBe("16/11 09:05");
  });
});
