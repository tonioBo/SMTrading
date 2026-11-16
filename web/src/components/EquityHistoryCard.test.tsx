import { QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import type { EquityHistory } from "@/api/hooks/equityHistory";
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
  it("converts to % since the first point, with the goal relative to it", () => {
    const lines = historyLines(history(), "pct", false);
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
    const lines = historyLines(history(), "eur", false);
    expect(lines[0].points.map((p) => p.v)).toEqual([250, 255]);
    expect(lines[1].points.map((p) => p.v)).toEqual([259.08, 259.08]);
  });
});

describe("EquityHistoryCard", () => {
  it("dashboard: every portfolio in %, legend with the change, goal on 1s, €/% toggle", async () => {
    renderCard();
    const legend = await screen.findByRole("list", { name: "Legenda" });
    expect(requests[0].get("range")).toBe("1w");
    expect(requests[0].has("portfolio_id")).toBe(false);
    expect(within(legend).getByText("Prudente")).toBeInTheDocument();
    expect(within(legend).getByText("-10,00 €")).toBeInTheDocument();
    expect(within(legend).queryByText("Vuoto")).toBeNull();
    expect(within(legend).getByText(/Obiettivo settimana \+2%/)).toBeInTheDocument();
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
    await screen.findByRole("list", { name: "Legenda" });
    expect(screen.queryByText(/Obiettivo settimana/)).toBeNull();
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
