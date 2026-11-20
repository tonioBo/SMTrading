import { QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { HOURLY_RANGES, type EquityHistory, type EquityHistoryGoalLine } from "@/api/hooks/equityHistory";
import { EquityHistoryCard } from "@/components/EquityHistoryCard";
import { readPalette } from "@/lib/chartTheme";
import { equitySeries, historyLines, toSeriesData, tooltipRows, totalChange, totalPoints } from "@/lib/equityHistory";
import { createQueryClient } from "@/queryClient";
import { REAL_HISTORY } from "@/test/fixtures/equityHistory";
import { charts, FakeSeries, type FakeChart } from "@/test/lightweightCharts";

const requests: URLSearchParams[] = [];

const weekly = (range: string) => range === "1w" || range === "wtd";

function history(range = "1w"): EquityHistory {
  return {
    range: range as EquityHistory["range"],
    start: "2026-11-09T12:00:00Z",
    end: "2026-11-16T12:00:00Z",
    goal_pct: weekly(range) ? "2.0" : null,
    portfolios: [
      {
        id: "p1",
        name: "Prudente",
        series: [
          { ts: "2026-11-10T08:00:00Z", equity_eur: "250.00" },
          { ts: "2026-11-16T11:55:00Z", equity_eur: "255.00" },
        ],
        first_eur: "250.00",
        last_eur: "255.00",
        change_eur: "5.00",
        change_pct: "2.00",
        goal: weekly(range) ? { week_start: "2026-11-15T23:00:00Z", base_eur: "254.00", target_eur: "259.08" } : null,
      },
      {
        id: "p2",
        name: "Aggressivo",
        series: [
          { ts: "2026-11-10T08:00:00Z", equity_eur: "1000.00" },
          { ts: "2026-11-16T11:55:00Z", equity_eur: "990.00" },
        ],
        first_eur: "1000.00",
        last_eur: "990.00",
        change_eur: "-10.00",
        change_pct: "-1.00",
        goal: null,
      },
      { id: "p3", name: "Vuoto", series: [], first_eur: null, last_eur: null, change_eur: null, change_pct: null, goal: null },
    ],
  };
}

function goalLine(over: Partial<EquityHistoryGoalLine>): EquityHistoryGoalLine {
  return {
    goal_id: "g1", name: "settimanale", horizon: "week", scope: "total", portfolio_id: null, kind: "min", target_pct: "2",
    period_start: "2026-11-15T23:00:00Z", period_end: "2026-11-22T23:00:00Z", base_eur: "1254.00", target_eur: "1279.08", ...over,
  };
}

/** The server with the `goals` list: the weekly (total) goal and a cap on «Aggressivo». */
function withGoals(range = "1w", portfolioId?: string): EquityHistory {
  const h = history(range);
  const cap = goalLine({
    goal_id: "g2", name: "Natale", horizon: "custom", scope: "portfolio", portfolio_id: "p2", kind: "cap", target_pct: "5",
    period_start: "2026-10-31T23:00:00Z", period_end: "2026-12-24T23:00:00Z", base_eur: "980.00", target_eur: "1029.00",
  });
  if (portfolioId) {
    return { ...h, portfolios: h.portfolios.filter((p) => p.id === portfolioId), goals: portfolioId === "p2" ? [cap] : [] };
  }
  return { ...h, goals: [goalLine({}), cap] };
}

const server = setupServer(
  http.get("*/api/portfolios/equity-history", ({ request }) => {
    const qs = new URL(request.url).searchParams;
    requests.push(qs);
    return HttpResponse.json(history(qs.get("range") ?? "1m"));
  }),
);
beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  server.resetHandlers();
  requests.length = 0;
});
afterAll(() => server.close());

function renderCard(portfolioId?: string) {
  return render(
    <QueryClientProvider client={createQueryClient()}>
      <EquityHistoryCard portfolioId={portfolioId} />
    </QueryClientProvider>,
  );
}

describe("historyLines", () => {
  it("converts to % since the first point, with the weekly goal relative to it (portfolio detail)", () => {
    const lines = historyLines(history(), "pct", true);
    expect(lines.map((l) => l.key)).toEqual(["p1", "p1-goal", "p2"]);
    expect(lines[0].points.map((p) => p.v)).toEqual([0, 2]);
    expect(lines[2].points.map((p) => p.v)).toEqual([0, -1]);
    const goal = lines[1];
    expect(goal.reference).toBe(true);
    expect(goal.points[0].t).toBe(Date.parse("2026-11-15T23:00:00Z"));
    expect(goal.points[1].t).toBe(Date.parse("2026-11-16T12:00:00Z"));
    expect(goal.points[0].v).toBeCloseTo(3.632, 3); // 259.08 / 250 - 1
  });
  it("wtd: the «Totale» ends on Σ last / Σ first − 1 (the weekly total goal's %)", () => {
    const lines = historyLines(withGoals("wtd"), "pct", false);
    const total = lines.find((l) => l.key === "total")!;
    expect(total.points.at(-1)!.v).toBeCloseTo((255 + 990) / (250 + 1000) * 100 - 100, 10);
    expect(totalChange(withGoals("wtd"))?.change_pct).toBe("-0.40");
    expect(lines.map((l) => l.key)).toContain("goal-g1");
  });
  it("keeps euros", () => {
    const lines = historyLines(history(), "eur", true);
    expect(lines[0].points.map((p) => p.v)).toEqual([250, 255]);
    expect(lines[1].points.map((p) => p.v)).toEqual([259.08, 259.08]);
  });
});

describe("historyLines with the goals list", () => {
  it("dashboard %: one line per goal (total vs the total at the range start), no per-portfolio weekly duplicates", () => {
    const lines = historyLines(withGoals(), "pct", false);
    expect(lines.map((l) => l.key)).toEqual(["p1", "p2", "total", "goal-g1", "goal-g2"]);
    expect(lines[2]).toMatchObject({ label: "Totale", bold: true });
    expect(lines[2].points.map((p) => p.v)).toEqual([0, -0.4]); // 1245 / 1250 - 1
    const total = lines[3];
    expect(total.reference).toBe(true);
    expect(total.tag).toBe("settimanale +2%");
    expect(total.points[0].t).toBe(Date.parse("2026-11-15T23:00:00Z"));
    expect(total.points[1].t).toBe(Date.parse("2026-11-16T12:00:00Z")); // until now, not the period end
    expect(total.points[0].v).toBeCloseTo(2.326, 3); // 1279.08 / (250 + 1000) - 1
    const cap = lines[4];
    expect(cap.tag).toBe("Natale tetto +5% · Aggressivo");
    expect(cap.color).toBe(lines[1].color); // the portfolio's colour
    expect(cap.points[0].t).toBe(Date.parse("2026-11-10T08:00:00Z")); // from the first point
    expect(cap.points[0].v).toBeCloseTo(2.9, 3); // 1029 / 1000 - 1
  });

  it("dashboard €: the «Totale» in euros, the total goal at its target against it, portfolio goals in euros", () => {
    const lines = historyLines(withGoals(), "eur", false);
    expect(lines.map((l) => l.key)).toEqual(["p1", "p2", "total", "goal-g1", "goal-g2"]);
    expect(lines[2].points.map((p) => p.v)).toEqual([1250, 1245]);
    expect(lines[3].points.map((p) => p.v)).toEqual([1279.08, 1279.08]);
    expect(lines[4].points.map((p) => p.v)).toEqual([1029, 1029]);
  });

  it("no «Totale» with one portfolio with data (nor on the portfolio detail): total goals in % only", () => {
    const h = withGoals();
    h.portfolios = h.portfolios.filter((p) => p.id !== "p2");
    expect(historyLines(h, "eur", false).map((l) => l.key)).toEqual(["p1"]);
    expect(historyLines(h, "pct", false).map((l) => l.key)).toEqual(["p1", "goal-g1"]);
    expect(totalChange(h)).toBeNull();
    expect(historyLines(history(), "pct", true).some((l) => l.key === "total")).toBe(false);
  });

  it("a portfolio out of the totals is drawn but left out of the «Totale» and of the total goal's base", () => {
    const h = withGoals();
    const p3 = {
      id: "p4",
      name: "Sfida",
      in_totals: false,
      series: [
        { ts: "2026-11-10T08:00:00Z", equity_eur: "500.00" },
        { ts: "2026-11-16T11:55:00Z", equity_eur: "600.00" },
      ],
      first_eur: "500.00",
      last_eur: "600.00",
      change_eur: "100.00",
      change_pct: "20.00",
      goal: null,
    };
    h.portfolios = [...h.portfolios, p3];
    const eur = historyLines(h, "eur", false);
    expect(eur.map((l) => l.key)).toEqual(["p1", "p2", "p4", "total", "goal-g1", "goal-g2"]);
    expect(eur.find((l) => l.key === "total")!.points.map((p) => p.v)).toEqual([1250, 1245]);
    expect(eur.find((l) => l.key === "total")!.label).toBe("Totale (obiettivi)");
    expect(historyLines(withGoals(), "eur", false).find((l) => l.key === "total")!.label).toBe("Totale");
    const pct = historyLines(h, "pct", false);
    // base = 250 + 1000 (the counted portfolios' first points): 1279.08 / 1250 - 1
    expect(pct.find((l) => l.key === "goal-g1")!.points[0].v).toBeCloseTo(2.3264, 4);
    expect(totalChange(h)).toEqual({ change_eur: "-5.00", change_pct: "-0.40" });
    // only one counted portfolio left: no «Totale»
    h.portfolios = h.portfolios.filter((p) => p.id !== "p2");
    expect(historyLines(h, "eur", false).some((l) => l.key === "total")).toBe(false);
    expect(totalChange(h)).toBeNull();
  });

  it("goals without data, or whose period ended before the shown points, are skipped", () => {
    const h = withGoals();
    h.goals = [goalLine({ base_eur: null, target_eur: null }), goalLine({ goal_id: "g9", period_end: "2026-11-09T23:00:00Z" })];
    expect(historyLines(h, "pct", false).map((l) => l.key)).toEqual(["p1", "p2", "total"]);
  });

  it("portfolio detail: its weekly goal line (as before) plus its own goals", () => {
    const one = withGoals("1w", "p1");
    expect(historyLines(one, "eur", true).map((l) => l.key)).toEqual(["p1", "p1-goal"]);
    const h = history("1w");
    const two: EquityHistory = { ...withGoals("1w", "p2"), portfolios: [{ ...h.portfolios[1], goal: { week_start: "2026-11-15T23:00:00Z", base_eur: "980", target_eur: "999.60" } }] };
    const lines = historyLines(two, "eur", true);
    expect(lines.map((l) => l.key)).toEqual(["p2", "p2-goal", "goal-g2"]);
    expect(lines[1].tag).toBe("settimanale +2%");
    expect(lines[2].tag).toBe("Natale tetto +5%");
  });
});

/** The dashboard with an index goal on the total and its benchmark series. */
function withBenchmark(): EquityHistory {
  const h = withGoals();
  const index = goalLine({
    goal_id: "g5", name: "indice", horizon: "quarter", kind: "beat_index", benchmark: "VWCE.DE", target_pct: "0",
    period_start: "2026-09-30T22:00:00Z", period_end: "2026-12-31T23:00:00Z", base_eur: "1250.00", target_eur: null,
  });
  return {
    ...h,
    goals: [...(h.goals ?? []), index],
    benchmarks: {
      "VWCE.DE": [
        { ts: "2026-11-10T08:00:00Z", pct: "0.5" },
        { ts: "2026-11-16T11:55:00Z", pct: "1.25" },
      ],
    },
  };
}

describe("totalPoints", () => {
  const a = [
    { t: 0, v: 100 },
    { t: 10, v: 110 },
    { t: 20, v: 120 },
  ];
  const b = [
    { t: 5, v: 400 },
    { t: 20, v: 380 },
  ];
  it("€: the sum on every timestamp, each portfolio carried forward, counted from its first point", () => {
    expect(totalPoints([a, b], "eur")).toEqual([
      { t: 0, v: 100 },
      { t: 5, v: 500 },
      { t: 10, v: 510 },
      { t: 20, v: 500 },
    ]);
  });
  it("%: on the sum of the first points of the counted portfolios (a late one makes no jump)", () => {
    const pts = totalPoints([a, b], "pct");
    expect(pts.map((p) => p.t)).toEqual([0, 5, 10, 20]);
    expect(pts[0].v).toBe(0);
    expect(pts[1].v).toBe(0); // b joins at its own first value
    expect(pts[2].v).toBe(2); // 510 / 500 - 1
    expect(pts[3].v).toBe(0); // (120 + 380) / (100 + 400) - 1 = Σ last / Σ first - 1
  });
  it("the legend's change: Σ change on Σ first", () => {
    expect(totalChange(history())).toEqual({ change_eur: "-5.00", change_pct: "-0.40" });
  });
});

describe("historyLines with the benchmarks", () => {
  it("% mode: a thin, solid, labelled line per index; the index goal itself draws no flat line", () => {
    const lines = historyLines(withBenchmark(), "pct", false);
    expect(lines.map((l) => l.key)).toEqual(["p1", "p2", "total", "goal-g1", "goal-g2", "bench-VWCE.DE"]);
    const b = lines.at(-1)!;
    expect(b).toMatchObject({ label: "Indice VWCE", tag: "VWCE", thin: true });
    expect(b.reference).toBeFalsy();
    // rebased to its first point in the range, like the portfolios: ((1.0125 / 1.005) - 1) × 100
    expect(b.points.map((p) => p.t)).toEqual([Date.parse("2026-11-10T08:00:00Z"), Date.parse("2026-11-16T11:55:00Z")]);
    expect(b.points[0].v).toBe(0);
    expect(b.points[1].v).toBeCloseTo(0.7463, 4);
  });

  it("a series already starting at 0 (the period start in the range) stays as it is", () => {
    const h = withBenchmark();
    h.benchmarks = { SPY: [{ ts: "2026-11-10T08:00:00Z", pct: "0" }, { ts: "2026-11-16T11:55:00Z", pct: "-2.5" }] };
    const b = historyLines(h, "pct", false).at(-1)!;
    expect(b.points.map((p) => p.v)).toEqual([0, -2.5]);
  });

  it("€ mode and responses without benchmarks: no index line", () => {
    expect(historyLines(withBenchmark(), "eur", false).some((l) => l.key.startsWith("bench-"))).toBe(false);
    expect(historyLines(withGoals(), "pct", false).some((l) => l.key.startsWith("bench-"))).toBe(false);
  });
});

describe("chart series and tooltip", () => {
  const palette = readPalette();
  it("areas first, then the indices (thin), the «Totale» (bold), the goals as price lines", () => {
    const series = equitySeries(historyLines(withBenchmark(), "pct", false), palette, false);
    expect(series.map((s) => [s.key, s.kind, s.lineWidth])).toEqual([
      ["p1", "area", 2],
      ["p2", "area", 2],
      ["bench-VWCE.DE", "line", 1],
      ["total", "line", 3],
      ["goal-g1", "goal", 1],
      ["goal-g2", "goal", 1],
    ]);
    expect(series[3].color).toBe(palette.fg); // the token resolved for the canvas
    expect(series[4].priceLine).toMatchObject({ title: "settimanale +2%", color: palette.warn });
    expect(series[5].priceLine?.price).toBeCloseTo(2.9, 3);
  });
  it("chart data: seconds, ascending, one point per second", () => {
    expect(toSeriesData([{ t: 2500, v: 2 }, { t: 1000, v: 1 }, { t: 2900, v: 3 }, { t: NaN, v: 1 }])).toEqual([
      { time: 1, value: 1 },
      { time: 2, value: 3 },
    ]);
  });
  it("the tooltip: euros and the change in € and % for each series, an index in %, no goals", () => {
    const h = withBenchmark();
    const pct = historyLines(h, "pct", false);
    const eur = historyLines(h, "eur", false);
    const rows = tooltipRows(pct, eur, pct, Date.parse("2026-11-16T11:00:00Z"));
    expect(rows.map((r) => [r.label, r.value, r.change])).toEqual([
      ["Prudente", "255,00 €", "+5,00 € · +2,00%"],
      ["Aggressivo", "990,00 €", "-10,00 € · -1,00%"],
      ["Totale", "1.245,00 €", "-5,00 € · -0,40%"],
      ["Indice VWCE", "+0,75%", null],
    ]);
    expect(rows[2].bold).toBe(true);
    // the first point: no change yet
    expect(tooltipRows(pct, eur, pct, 0)[0]).toMatchObject({ value: "250,00 €", change: "0,00 € · 0,00%" });
  });
});

/** The chart drawn (the Lightweight Charts stand-in of src/test), once loaded. */
async function drawn(): Promise<FakeChart> {
  await screen.findByTestId("equity-chart");
  await waitFor(() => expect(charts.at(-1)?.series.length ?? 0).toBeGreaterThan(0));
  return charts.at(-1)!;
}
const lineSeries = (c: FakeChart, width: number) => c.series.filter((s) => s.type === "Line" && s.options.lineWidth === width && s.options.lineVisible !== false);
const goalTitles = (c: FakeChart) => c.series.flatMap((s) => s.priceLines.map((l) => l.options.title));
const sec = (iso: string) => Date.parse(iso) / 1000;

describe("EquityHistoryCard", () => {
  afterEach(() => {
    charts.length = 0;
  });

  it("dashboard with an index goal: the index line is thin, tagged and in the legend", async () => {
    server.use(http.get("*/api/portfolios/equity-history", () => HttpResponse.json(withBenchmark())));
    renderCard();
    const legend = await screen.findByRole("list", { name: "Legenda" });
    expect(within(legend).getByText("Indice VWCE")).toBeInTheDocument();
    const c = await drawn();
    const [bench] = lineSeries(c, 1);
    expect(bench.data.map((p) => (p as { time: number }).time)).toEqual([sec("2026-11-10T08:00:00Z"), sec("2026-11-16T11:55:00Z")]);
    expect(bench.options.lastValueVisible).toBe(false);
    await userEvent.click(screen.getByRole("button", { name: "€" }));
    await waitFor(() => expect(lineSeries(c, 1)).toHaveLength(0));
    expect(screen.queryByText("Indice VWCE")).toBeNull();
  });

  it("dashboard with the goals list: labelled dashed price lines in the chart, and the legend", async () => {
    server.use(http.get("*/api/portfolios/equity-history", () => HttpResponse.json(withGoals())));
    renderCard();
    const legend = await screen.findByRole("list", { name: "Legenda" });
    expect(within(legend).getByText("Obiettivo settimanale +2%")).toBeInTheDocument();
    expect(within(legend).getByText("Obiettivo Natale tetto +5% · Aggressivo")).toBeInTheDocument();
    const c = await drawn();
    expect(goalTitles(c)).toEqual(["settimanale +2%", "Natale tetto +5% · Aggressivo"]);
    const weekly = c.series.find((s) => s.priceLines[0]?.options.title === "settimanale +2%")!;
    expect(weekly.options.lineVisible).toBe(false); // only its price line shows (and counts in the autoscale)
    expect(weekly.priceLines[0].options).toMatchObject({ lineStyle: 2, axisLabelVisible: true });
    expect(weekly.priceLines[0].options.price as number).toBeCloseTo(2.326, 3);
  });

  it("dashboard: every portfolio in % as gradient areas, legend with the change, crosshair tooltip, €/% toggle", async () => {
    server.use(
      http.get("*/api/portfolios/equity-history", ({ request }) => {
        const qs = new URL(request.url).searchParams;
        requests.push(qs);
        return HttpResponse.json(withGoals("1w"));
      }),
    );
    renderCard();
    const legend = await screen.findByRole("list", { name: "Legenda" });
    expect(requests[0].get("range")).toBe("1d"); // the default range: one day
    expect(requests[0].has("portfolio_id")).toBe(false);
    expect(within(legend).getByText("Prudente")).toBeInTheDocument();
    expect(within(legend).getByText("-10,00 €")).toBeInTheDocument();
    expect(within(legend).queryByText("Vuoto")).toBeNull();
    expect(within(legend).getByText("Obiettivo settimanale +2%")).toBeInTheDocument();
    expect(within(legend).getByText("variazione del valore (include depositi/prelievi)")).toBeInTheDocument();
    expect(within(legend).getByText("Prudente").closest("li")).toHaveAttribute("title", "variazione del valore (include depositi/prelievi)");
    expect(screen.getByRole("img", { name: /dei portafogli \(%\)/ })).toBeInTheDocument();
    const c = await drawn();
    const areas = c.series.filter((s) => s.type === "Area");
    expect(areas).toHaveLength(2);
    expect(areas[0].options).toMatchObject({ lineColor: "#10b981", topColor: "rgba(16, 185, 129, 0.12)", bottomColor: "rgba(16, 185, 129, 0)" });
    expect(areas[1].data).toEqual([
      { time: sec("2026-11-10T08:00:00Z"), value: 0 },
      { time: sec("2026-11-16T11:55:00Z"), value: -1 },
    ]);
    const fmt = (areas[0].options.priceFormat as { formatter: (v: number) => string }).formatter;
    expect(fmt(2)).toBe("+2,00%");

    // the crosshair: every series' value in euros and its change in € and %
    act(() => c.moveCrosshair(sec("2026-11-16T11:55:00Z")));
    const tip = screen.getByRole("tooltip");
    expect(within(tip).getByText("16/11 12:55")).toBeInTheDocument();
    expect(within(tip).getByText("255,00 €")).toBeInTheDocument();
    expect(within(tip).getByText("+5,00 € · +2,00%")).toBeInTheDocument();
    expect(within(tip).getByText("-10,00 € · -1,00%")).toBeInTheDocument();
    expect(within(tip).queryByText(/settimanale/)).toBeNull(); // goals are not in the tooltip
    act(() => c.moveCrosshair());
    expect(screen.queryByRole("tooltip")).toBeNull();

    await userEvent.click(screen.getByRole("button", { name: "€" }));
    expect(screen.getByRole("img", { name: /dei portafogli \(€\)/ })).toBeInTheDocument();
    await waitFor(() => expect(c.series.find((s) => s.type === "Area")?.data[0]).toEqual({ time: sec("2026-11-10T08:00:00Z"), value: 250 }));
    const area = c.series.find((s) => s.type === "Area")!;
    expect((area.options.priceFormat as { formatter: (v: number) => string }).formatter(1250)).toBe("1.250 €");
    act(() => c.moveCrosshair(sec("2026-11-16T11:55:00Z")));
    expect(within(screen.getByRole("tooltip")).getByText("990,00 €")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "1m" }));
    await screen.findByRole("button", { name: "1m", pressed: true });
    expect(requests.at(-1)?.get("range")).toBe("1m");
    // the goals come with every range; in € the total goal is drawn against the «Totale»
    expect(await screen.findByText("Obiettivo Natale tetto +5% · Aggressivo")).toBeInTheDocument();
    expect(screen.getByText("Obiettivo settimanale +2%")).toBeInTheDocument();
  });

  it("dashboard: the «Totale» first in the legend with Σ change, a bold line, in the tooltip", async () => {
    server.use(http.get("*/api/portfolios/equity-history", () => HttpResponse.json(withGoals())));
    renderCard();
    const legend = await screen.findByRole("list", { name: "Legenda" });
    const rows = within(legend).getAllByRole("listitem");
    expect(rows[0]).toHaveTextContent("Totale");
    expect(within(rows[0]).getByText("-5,00 €")).toBeInTheDocument();
    expect(rows[0]).toHaveTextContent("(-0,40%)");
    expect(rows[1]).toHaveTextContent("Prudente");
    const c = await drawn();
    const [total] = lineSeries(c, 3);
    expect(total.data.map((p) => (p as { value: number }).value)).toEqual([0, -0.4]);
    // drawn after the areas: on top
    const lastArea = Math.max(...c.series.map((s, i) => (s.type === "Area" ? i : -1)));
    expect(c.series.indexOf(total)).toBeGreaterThan(lastArea);
    act(() => c.moveCrosshair(sec("2026-11-16T11:55:00Z")));
    const tip = screen.getByRole("tooltip");
    expect(within(tip).getByText("1.245,00 €")).toBeInTheDocument();
    expect(within(tip).getByText("-5,00 € · -0,40%")).toBeInTheDocument();
  });

  it("the real responses: switching to a range not loaded yet (the chart unmounts while loading) does not crash, in % and €", async () => {
    // the 20/11 «6h» crash: on unmount the chart was removed before its series («Value is undefined»)
    server.use(
      http.get("*/api/portfolios/equity-history", async ({ request }) => {
        const range = new URL(request.url).searchParams.get("range") as keyof typeof REAL_HISTORY;
        await new Promise((r) => setTimeout(r, 5));
        return HttpResponse.json(REAL_HISTORY[range]);
      }),
    );
    renderCard();
    await drawn();
    for (const [label, unit] of [["2h", "%"], ["6h", "%"], ["12h", "€"], ["6h", "€"], ["1g", "%"], ["6h", "%"]]) {
      await userEvent.click(screen.getByRole("button", { name: unit }));
      await userEvent.click(screen.getByRole("button", { name: label }));
      await screen.findByRole("button", { name: label, pressed: true });
      const c = await drawn();
      expect(c.series.filter((s) => s.type === "Area")).toHaveLength(3);
      expect(screen.getByText("Totale (obiettivi)")).toBeInTheDocument();
      expect(screen.queryByText("Grafico non disponibile")).toBeNull();
    }
  });

  it("data the chart refuses: «Grafico non disponibile» in the card, the rest of the page stays", async () => {
    const error = vi.spyOn(console, "error").mockImplementation(() => {});
    const setData = vi.spyOn(FakeSeries.prototype, "setData").mockImplementation(() => {
      throw new Error("Value is undefined");
    });
    try {
      server.use(http.get("*/api/portfolios/equity-history", () => HttpResponse.json(REAL_HISTORY["6h"])));
      renderCard();
      expect(await screen.findByText("Grafico non disponibile")).toBeInTheDocument();
      expect(screen.getByRole("list", { name: "Legenda" })).toBeInTheDocument();
      expect(charts.at(-1)!.series).toHaveLength(0); // what was added is taken away again
      setData.mockRestore();
      await userEvent.click(screen.getByRole("button", { name: "€" })); // another unit: tried again
      await waitFor(() => expect(screen.queryByText("Grafico non disponibile")).toBeNull());
      expect((await drawn()).series.length).toBeGreaterThan(0);
    } finally {
      setData.mockRestore();
      error.mockRestore();
    }
  });

  it("portfolio detail: one green area when up (stronger gradient), red when down; removed on unmount", async () => {
    server.use(
      http.get("*/api/portfolios/equity-history", ({ request }) => {
        const id = new URL(request.url).searchParams.get("portfolio_id");
        const h = history();
        return HttpResponse.json({ ...h, portfolios: h.portfolios.filter((p) => p.id === id) });
      }),
    );
    const { unmount } = renderCard("p1");
    const c = await drawn();
    const [area] = c.series;
    expect(area.type).toBe("Area");
    expect(area.options).toMatchObject({ lineColor: "rgb(16, 185, 129)", topColor: "rgba(16, 185, 129, 0.35)" });
    unmount();
    expect(c.removed).toBe(true);
    charts.length = 0;
    renderCard("p2"); // 1000 -> 990
    const down = await drawn();
    expect(down.series[0].options.lineColor).toBe("rgb(244, 63, 94)");
  });

  it("«Sett.»: after «1g», asks for the current week, dates on the axis, the weekly goal drawn", async () => {
    renderCard("p1");
    await screen.findByRole("list", { name: "Legenda" });
    const labels = within(screen.getByRole("group", { name: "Periodo" })).getAllByRole("button").map((b) => b.textContent);
    expect(labels.slice(labels.indexOf("1g"), labels.indexOf("1g") + 3)).toEqual(["1g", "Sett.", "1s"]);
    await userEvent.click(screen.getByRole("button", { name: "Sett." }));
    await screen.findByRole("button", { name: "Sett.", pressed: true });
    expect(requests.at(-1)?.get("range")).toBe("wtd");
    expect(await screen.findByText("Obiettivo settimanale +2%", {}, { timeout: 5000 })).toBeInTheDocument();
    expect(HOURLY_RANGES.has("wtd")).toBe(false);
  });

  it("portfolio detail: that portfolio in €, no unit toggle", async () => {
    renderCard("p1");
    const legend = await screen.findByRole("list", { name: "Legenda" });
    expect(within(legend).queryByText("Totale")).toBeNull();
    expect(screen.queryByTestId("line-total")).toBeNull();
    expect(requests[0].get("portfolio_id")).toBe("p1");
    expect(screen.queryByRole("group", { name: "Unità" })).toBeNull();
    expect(screen.getByRole("img", { name: /del portafoglio \(€\)/ })).toBeInTheDocument();
  });

  it("empty and error states", async () => {
    server.use(http.get("*/api/portfolios/equity-history", () => HttpResponse.json({ ...history(), portfolios: [] })));
    const { unmount } = renderCard();
    expect(await screen.findByText("Nessun dato nel periodo")).toBeInTheDocument();
    unmount();
    server.use(http.get("*/api/portfolios/equity-history", () => HttpResponse.json({ code: "INTERNAL", message: "x" }, { status: 500 })));
    renderCard();
    expect(await screen.findByText("Impossibile caricare l'andamento", {}, { timeout: 5000 })).toBeInTheDocument();
  });
});
