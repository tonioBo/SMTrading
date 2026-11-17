import { QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import type { EquityHistory, EquityHistoryGoalLine } from "@/api/hooks/equityHistory";
import { EquityHistoryCard } from "@/components/EquityHistoryCard";
import { historyLines } from "@/lib/equityHistory";
import { createQueryClient } from "@/queryClient";

const requests: URLSearchParams[] = [];

function history(range = "1w"): EquityHistory {
  return {
    range: range as EquityHistory["range"],
    start: "2026-11-09T12:00:00Z",
    end: "2026-11-16T12:00:00Z",
    goal_pct: range === "1w" ? "2.0" : null,
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
        goal: range === "1w" ? { week_start: "2026-11-15T23:00:00Z", base_eur: "254.00", target_eur: "259.08" } : null,
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
  it("keeps euros", () => {
    const lines = historyLines(history(), "eur", true);
    expect(lines[0].points.map((p) => p.v)).toEqual([250, 255]);
    expect(lines[1].points.map((p) => p.v)).toEqual([259.08, 259.08]);
  });
});

describe("historyLines with the goals list", () => {
  it("dashboard %: one line per goal (total vs the total at the range start), no per-portfolio weekly duplicates", () => {
    const lines = historyLines(withGoals(), "pct", false);
    expect(lines.map((l) => l.key)).toEqual(["p1", "p2", "goal-g1", "goal-g2"]);
    const total = lines[2];
    expect(total.reference).toBe(true);
    expect(total.tag).toBe("settimanale +2%");
    expect(total.points[0].t).toBe(Date.parse("2026-11-15T23:00:00Z"));
    expect(total.points[1].t).toBe(Date.parse("2026-11-16T12:00:00Z")); // until now, not the period end
    expect(total.points[0].v).toBeCloseTo(2.326, 3); // 1279.08 / (250 + 1000) - 1
    const cap = lines[3];
    expect(cap.tag).toBe("Natale tetto +5% · Aggressivo");
    expect(cap.color).toBe(lines[1].color); // the portfolio's colour
    expect(cap.points[0].t).toBe(Date.parse("2026-11-10T08:00:00Z")); // from the first point
    expect(cap.points[0].v).toBeCloseTo(2.9, 3); // 1029 / 1000 - 1
  });

  it("dashboard €: portfolio goals in euros, total goals left out (not comparable with one portfolio)", () => {
    const lines = historyLines(withGoals(), "eur", false);
    expect(lines.map((l) => l.key)).toEqual(["p1", "p2", "goal-g2"]);
    expect(lines[2].points.map((p) => p.v)).toEqual([1029, 1029]);
  });

  it("goals without data, or whose period ended before the shown points, are skipped", () => {
    const h = withGoals();
    h.goals = [goalLine({ base_eur: null, target_eur: null }), goalLine({ goal_id: "g9", period_end: "2026-11-09T23:00:00Z" })];
    expect(historyLines(h, "pct", false).map((l) => l.key)).toEqual(["p1", "p2"]);
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

describe("historyLines with the benchmarks", () => {
  it("% mode: a thin, solid, labelled line per index; the index goal itself draws no flat line", () => {
    const lines = historyLines(withBenchmark(), "pct", false);
    expect(lines.map((l) => l.key)).toEqual(["p1", "p2", "goal-g1", "goal-g2", "bench-VWCE.DE"]);
    const b = lines.at(-1)!;
    expect(b).toMatchObject({ label: "Indice VWCE", tag: "VWCE", thin: true });
    expect(b.reference).toBeFalsy();
    expect(b.points).toEqual([
      { t: Date.parse("2026-11-10T08:00:00Z"), v: 0.5 },
      { t: Date.parse("2026-11-16T11:55:00Z"), v: 1.25 },
    ]);
  });

  it("€ mode and responses without benchmarks: no index line", () => {
    expect(historyLines(withBenchmark(), "eur", false).some((l) => l.key.startsWith("bench-"))).toBe(false);
    expect(historyLines(withGoals(), "pct", false).some((l) => l.key.startsWith("bench-"))).toBe(false);
  });
});

describe("EquityHistoryCard", () => {
  it("dashboard with an index goal: the index line is thin, tagged and in the legend", async () => {
    server.use(http.get("*/api/portfolios/equity-history", () => HttpResponse.json(withBenchmark())));
    renderCard();
    const legend = await screen.findByRole("list", { name: "Legenda" });
    expect(within(legend).getByText("Indice VWCE")).toBeInTheDocument();
    const path = screen.getByTestId("line-bench-VWCE.DE");
    expect(path).toHaveAttribute("stroke-width", "1");
    expect(path).not.toHaveAttribute("stroke-dasharray");
    expect(screen.getByTestId("tag-bench-VWCE.DE")).toHaveTextContent("VWCE");
    await userEvent.click(screen.getByRole("button", { name: "€" }));
    expect(screen.queryByTestId("line-bench-VWCE.DE")).toBeNull();
    expect(screen.queryByText("Indice VWCE")).toBeNull();
  });

  it("dashboard with the goals list: dashed lines labelled in the chart and the legend", async () => {
    server.use(http.get("*/api/portfolios/equity-history", () => HttpResponse.json(withGoals())));
    renderCard();
    const legend = await screen.findByRole("list", { name: "Legenda" });
    expect(within(legend).getByText("Obiettivo settimanale +2%")).toBeInTheDocument();
    expect(within(legend).getByText("Obiettivo Natale tetto +5% · Aggressivo")).toBeInTheDocument();
    expect(screen.getByTestId("line-goal-g1")).toHaveAttribute("stroke-dasharray");
    expect(screen.getByTestId("tag-goal-g2")).toHaveTextContent("Natale tetto +5% · Aggressivo");
  });

  it("dashboard: every portfolio in %, legend with the change, goals, €/% toggle", async () => {
    server.use(
      http.get("*/api/portfolios/equity-history", ({ request }) => {
        const qs = new URL(request.url).searchParams;
        requests.push(qs);
        return HttpResponse.json(withGoals(qs.get("range") ?? "1m"));
      }),
    );
    renderCard();
    const legend = await screen.findByRole("list", { name: "Legenda" });
    expect(requests[0].get("range")).toBe("1w");
    expect(requests[0].has("portfolio_id")).toBe(false);
    expect(within(legend).getByText("Prudente")).toBeInTheDocument();
    expect(within(legend).getByText("-10,00 €")).toBeInTheDocument();
    expect(within(legend).queryByText("Vuoto")).toBeNull();
    expect(within(legend).getByText("Obiettivo settimanale +2%")).toBeInTheDocument();
    expect(within(legend).getByText("variazione del valore (include depositi/prelievi)")).toBeInTheDocument();
    expect(within(legend).getByText("Prudente").closest("li")).toHaveAttribute("title", "variazione del valore (include depositi/prelievi)");
    expect(screen.getByRole("img", { name: /dei portafogli \(%\)/ })).toBeInTheDocument();
    expect(screen.getByText("+2%")).toBeInTheDocument(); // a y tick

    // hover: the tooltip lists each portfolio's nearest value (jsdom: no layout -> the end)
    fireEvent.pointerMove(screen.getByTestId("line-chart-plot"), { clientX: 10 });
    const tip = screen.getByRole("tooltip");
    expect(within(tip).getByText("16/11 12:55")).toBeInTheDocument();
    expect(within(tip).getByText("+2,00%")).toBeInTheDocument();
    expect(within(tip).getByText("-1,00%")).toBeInTheDocument();
    fireEvent.pointerLeave(screen.getByTestId("line-chart-plot"));
    expect(screen.queryByRole("tooltip")).toBeNull();

    await userEvent.click(screen.getByRole("button", { name: "€" }));
    expect(screen.getByRole("img", { name: /dei portafogli \(€\)/ })).toBeInTheDocument();
    fireEvent.pointerMove(screen.getByTestId("line-chart-plot"), { clientX: 10 });
    expect(within(screen.getByRole("tooltip")).getByText("990,00 €")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "1m" }));
    await screen.findByRole("button", { name: "1m", pressed: true });
    expect(requests.at(-1)?.get("range")).toBe("1m");
    // the goals come with every range; in € the total goal is left out
    expect(await screen.findByText("Obiettivo Natale tetto +5% · Aggressivo")).toBeInTheDocument();
    expect(screen.queryByText("Obiettivo settimanale +2%")).toBeNull();
  });

  it("portfolio detail: that portfolio in €, no unit toggle", async () => {
    renderCard("p1");
    await screen.findByRole("list", { name: "Legenda" });
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
