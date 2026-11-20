import { describe, expect, it } from "vitest";
import { readPalette } from "@/lib/chartTheme";
import { equitySeries, historyLines, toSeriesData, tooltipRows, type Unit } from "@/lib/equityHistory";
import type { ChartLine } from "@/lib/lineChart";
import { REAL_HISTORY } from "@/test/fixtures/equityHistory";

/** What Lightweight Charts accepts: finite values, times strictly ascending (no duplicates). */
function expectChartable(series: ReturnType<typeof equitySeries>) {
  for (const s of series) {
    expect(s.data.length, s.key).toBeGreaterThan(0);
    s.data.forEach((p, i) => {
      expect(Number.isFinite(p.value), `${s.key}[${i}] value ${p.value}`).toBe(true);
      expect(Number.isInteger(p.time), `${s.key}[${i}] time ${p.time}`).toBe(true);
      if (i > 0) expect(p.time, `${s.key}[${i}] time`).toBeGreaterThan(s.data[i - 1].time);
    });
    if (s.priceLine) expect(Number.isFinite(s.priceLine.price), `${s.key} price line`).toBe(true);
  }
}

describe("the real responses (2h, 6h, 12h, 1d) as chart data", () => {
  const palette = readPalette();
  for (const [range, data] of Object.entries(REAL_HISTORY)) {
    for (const unit of ["eur", "pct"] as Unit[]) {
      it(`${range} in ${unit}: every point finite, times ascending and unique; the tooltip has numbers`, () => {
        const lines = historyLines(data, unit, false);
        // three portfolios, the «Totale (obiettivi)» (Sfida is out of the totals), two goals
        expect(lines.map((l) => l.key)).toEqual([...data.portfolios.map((p) => p.id), "total", ...data.goals!.map((g) => `goal-${g.goal_id}`)]);
        expect(lines.find((l) => l.key === "total")?.label).toBe("Totale (obiettivi)");
        const series = equitySeries(lines, palette, false);
        expect(series).toHaveLength(lines.length);
        expectChartable(series);

        const eur = historyLines(data, "eur", false);
        const pct = historyLines(data, "pct", false);
        const times = [Date.parse(data.start!), Date.parse(data.end), ...lines.flatMap((l: ChartLine) => l.points.map((p) => p.t))];
        for (const t of times) {
          const rows = tooltipRows(lines, eur, pct, t);
          expect(rows).toHaveLength(4); // the portfolios and the total, not the goals
          for (const r of rows) expect(`${r.value} ${r.change}`).not.toMatch(/NaN|undefined|Infinity/);
        }
      });
    }
  }
});

describe("toSeriesData / equitySeries guards", () => {
  it("drops non-finite points, sorts, one point per second (the last wins)", () => {
    expect(
      toSeriesData([
        { t: 3_000, v: 3 },
        { t: 1_000, v: Number.NaN },
        { t: Number.NaN, v: 1 },
        { t: 2_000, v: 2 },
        { t: 2_400, v: 2.4 },
        { t: 1_000, v: Number.POSITIVE_INFINITY },
      ]),
    ).toEqual([
      { time: 2, value: 2.4 },
      { time: 3, value: 3 },
    ]);
  });
  it("a line with no finite point is not a series (no price line at a made-up 0)", () => {
    const lines: ChartLine[] = [
      { key: "a", label: "A", color: "#10b981", points: [{ t: 1_000, v: 1 }] },
      { key: "g", label: "G", color: "#f59e0b", points: [{ t: 1_000, v: Number.NaN }], reference: true },
    ];
    expect(equitySeries(lines, readPalette(), false).map((s) => s.key)).toEqual(["a"]);
  });
});
