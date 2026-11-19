import { describe, expect, it } from "vitest";
import { chartNumber, nearestIndex, signedNumber, timeLabel, toPctPoints } from "@/lib/lineChart";

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
    expect(signedNumber(2, 2)).toBe("+2,00");
    expect(signedNumber(-1.5, 1)).toBe("-1,5");
    expect(signedNumber(0.001, 2)).toBe("0,00");
  });
  it("formats times in Europe/Rome", () => {
    const t = Date.parse("2026-11-16T08:05:00Z");
    expect(timeLabel(t, "time")).toBe("09:05");
    expect(timeLabel(t, "day")).toBe("16/11");
    expect(timeLabel(t, "full")).toBe("16/11 09:05");
  });
});
