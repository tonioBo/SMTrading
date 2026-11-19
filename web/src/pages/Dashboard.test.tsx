import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import type { Dashboard, GoalProgress } from "@/api/types";
import type { LiveEvent } from "@/lib/ws";
import { renderApp } from "@/test/render";
import { commonHandlers } from "@/test/handlers";
import { haltFixture } from "@/test/halts";

const live = vi.hoisted(() => ({
  handlers: new Map<string, Set<(e: unknown) => void>>(),
  status: "open" as string,
  subscribed: [] as string[][],
}));
vi.mock("@/lib/ws", async () => {
  const { useEffect, useRef } = await import("react");
  const socket = {
    subscribeQuotes: (symbols: string[]) => {
      live.subscribed.push(symbols);
      return () => {};
    },
  };
  return {
    useLiveSocket: () => ({ socket, status: live.status }),
    // like the real hook: one subscription per mount and type, always the latest handler
    useLiveMessage: (type: string, handler: (e: unknown) => void) => {
      const ref = useRef(handler);
      ref.current = handler;
      useEffect(() => {
        const call = (e: unknown) => ref.current(e);
        if (!live.handlers.has(type)) live.handlers.set(type, new Set());
        live.handlers.get(type)!.add(call);
        return () => void live.handlers.get(type)?.delete(call);
      }, [type]);
    },
  };
});

function emit(event: LiveEvent) {
  act(() => live.handlers.get("event")?.forEach((h) => h(event)));
}

function tick(symbol: string) {
  act(() => live.handlers.get("quote")?.forEach((h) => h({ symbol, last: "1" })));
}

const dashboard: Dashboard = {
  total_equity_eur: "12345.60",
  day_pnl_eur: "-250.5",
  day_pnl_pct: "-1.99",
  portfolios: [
    { id: "p1", name: "Alfa", status: "active", mode: "simulazione", broker_kind: null, mode_label: "SMTrading (simulato)", equity_eur: "10000", day_pnl_eur: "-250.5", day_pnl_pct: "-2.4", stale_symbols: [], error: null },
    { id: "p2", name: "Beta", status: "active", mode: "paper", broker_kind: "alpaca_paper", mode_label: "Alpaca · prova", equity_eur: "2345.6", day_pnl_eur: "10", day_pnl_pct: "0.4", stale_symbols: [], error: null },
  ],
  modes: { simulazione: 1, paper: 1, reale: 0 },
  mode_counts: [
    { label: "SMTrading (simulato)", count: 1, live: false },
    { label: "Alpaca · prova", count: 1, live: false },
  ],
  connectors: [{ name: "binance", enabled: true, health_status: "ok", last_checked_at: null }],
  margin_alerts: [
    { id: 1, portfolio_id: "p1", kind: "margin_warning", payload: {}, created_at: "2026-11-12T10:00:00Z" },
  ],
  kill_switch: false,
  worker_heartbeat_age_s: 12,
};

let killSwitchOn = false;
let posted: unknown = null;
let current = dashboard;
let fetches = 0;
const server = setupServer(
  ...commonHandlers,
  http.get("*/api/auth/me", () => HttpResponse.json({ email: "admin@example.com", totp_enabled: false })),
  http.get("*/api/kill-switch", () => HttpResponse.json({ on: killSwitchOn })),
  http.post("*/api/kill-switch", async ({ request }) => {
    posted = await request.json();
    killSwitchOn = (posted as { on: boolean }).on;
    return HttpResponse.json({ on: killSwitchOn, canceled: 3 });
  }),
  http.get("*/api/dashboard", () => {
    fetches += 1;
    return HttpResponse.json(current);
  }),
);
beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  server.resetHandlers();
  killSwitchOn = false;
  posted = null;
  current = dashboard;
  fetches = 0;
  live.handlers.clear();
  live.status = "open";
  live.subscribed = [];
});
afterAll(() => server.close());

describe("Dashboard", () => {
  it("«Obiettivi»: every active goal compactly, with status and a link to the page", async () => {
    const base = {
      period_start: "2026-11-16", period_end: "2026-11-22", start_eur: "1000", current_eur: "1010", target_eur: "1020",
      gap_eur: "10", elapsed_pct: "40", as_of: "2026-11-17T09:00:00Z",
    };
    const goals: GoalProgress[] = [
      {
        ...base, goal_id: "g1", name: "settimanale", horizon: "week", scope: "total", portfolio_id: null, portfolio_name: null, kind: "min", pct: "1", target_pct: "2", status: "in linea",
        parts: [
          { portfolio_id: "p1", name: "Alfa", change_eur: "10.00", contribution_pct: "1.00" },
          { portfolio_id: "p2", name: "Beta", change_eur: "0.00", contribution_pct: "0.00" },
        ],
      },
      {
        ...base, goal_id: "g2", name: "Natale", horizon: "custom", scope: "portfolio", portfolio_id: "p1", portfolio_name: "Alfa", kind: "cap",
        period_start: "2026-11-01", period_end: "2026-12-24", pct: "6", target_pct: "5", status: "oltre il tetto",
      },
      { ...base, goal_id: "g3", name: "mensile", horizon: "month", scope: "total", portfolio_id: null, portfolio_name: null, kind: "min", start_eur: null, current_eur: null, target_eur: null, gap_eur: null, pct: null, target_pct: "4", status: "non iniziato" },
    ];
    server.use(http.get("*/api/goals/progress", () => HttpResponse.json(goals)));
    renderApp("/");
    const card = (await screen.findByText("Obiettivi", { selector: "h3, div" })).closest("div.rounded-lg") as HTMLElement;
    const list = await within(card).findByRole("list", { name: "Obiettivi attivi" });
    const rows = within(list).getAllByRole("listitem");
    expect(rows).toHaveLength(3);
    expect(within(rows[0]).getByText("settimanale")).toBeInTheDocument();
    expect(within(rows[0]).getByText("+2%")).toBeInTheDocument();
    expect(within(rows[0]).getByText("in linea")).toBeInTheDocument();
    expect(within(rows[0]).getByText("+1,00%")).toBeInTheDocument();
    expect(within(rows[0]).getByText(/Totale · 16\/11 – 22\/11/)).toBeInTheDocument();
    expect(within(rows[0]).getByRole("progressbar", { name: "Avanzamento settimanale" })).toHaveAttribute("aria-valuenow", "50");
    // the total goal's share of each portfolio, under the bar
    expect(within(rows[0]).getByTestId("goal-parts-settimanale")).toHaveTextContent("Alfa +10,00 € (+1,00 pt)· Beta 0,00 €");
    expect(within(rows[1]).queryByTestId("goal-parts-Natale")).toBeNull();
    expect(within(rows[1]).getByText("tetto +5%")).toBeInTheDocument();
    expect(within(rows[1]).getByText("oltre il tetto")).toBeInTheDocument();
    expect(within(rows[1]).getByText(/Alfa · 01\/11 – 24\/12/)).toBeInTheDocument();
    expect(within(rows[2]).getByText("non iniziato")).toBeInTheDocument();
    expect(within(rows[2]).queryByRole("progressbar")).not.toBeInTheDocument();
    expect(within(card).getByRole("link", { name: "Gestisci obiettivi" })).toHaveAttribute("href", "/obiettivi");
    expect(screen.queryByText("Obiettivo settimanale")).not.toBeInTheDocument();
  });

  it("«Obiettivi»: risk and index goals with their statuses, loss and comparison", async () => {
    const base = {
      period_start: "2026-11-01", period_end: "2026-11-30", start_eur: "1000", current_eur: "960", target_eur: "950",
      gap_eur: "10", elapsed_pct: "55", as_of: "2026-11-17T09:00:00Z", scope: "total" as const, portfolio_id: null, portfolio_name: null,
    };
    const goals: GoalProgress[] = [
      { ...base, goal_id: "g4", name: "rischio", horizon: "month", kind: "max_loss", pct: "-4", target_pct: "5", loss_pct: "4", status: "vicino al limite" },
      { ...base, goal_id: "g5", name: "rischio2", horizon: "month", kind: "max_loss", pct: "-6", target_pct: "5", loss_pct: "6", status: "superato" },
      {
        ...base, goal_id: "g6", name: "indice", horizon: "quarter", kind: "beat_index", pct: "1", target_pct: "0", target_eur: null, gap_eur: null,
        benchmark: "SPY", benchmark_pct: "2.5", diff_pp: "-1.5", status: "indietro",
      },
    ];
    server.use(http.get("*/api/goals/progress", () => HttpResponse.json(goals)));
    renderApp("/");
    const list = await screen.findByRole("list", { name: "Obiettivi attivi" });
    const rows = within(list).getAllByRole("listitem");
    expect(within(rows[0]).getByText("perdita max 5%")).toBeInTheDocument();
    expect(within(rows[0]).getByText("vicino al limite")).toHaveClass("text-warn");
    expect(within(rows[0]).getByText("perdita 4,00% su un limite di 5%")).toBeInTheDocument();
    expect(within(rows[0]).getByRole("progressbar", { name: "Perdita rischio" })).toHaveAttribute("aria-valuenow", "80");
    expect(within(rows[1]).getByText("superato")).toHaveClass("text-loss");
    expect(within(rows[2]).getByText("meglio di SPY")).toBeInTheDocument();
    expect(within(rows[2]).getByText("indietro")).toHaveClass("text-warn");
    expect(within(rows[2]).getByText("SPY +2,50% · differenza -1,50 pt")).toBeInTheDocument();
    expect(within(rows[2]).queryByRole("progressbar")).not.toBeInTheDocument();
  });

  it("«Obiettivi» is hidden without active goals", async () => {
    renderApp("/");
    expect(await screen.findByText("12.345,60 €")).toBeInTheDocument();
    expect(screen.queryByRole("list", { name: "Obiettivi attivi" })).not.toBeInTheDocument();
  });

  it("renders KPIs, a loss-styled negative P&L and portfolio links", async () => {
    renderApp("/");
    expect(await screen.findByText("12.345,60 €")).toBeInTheDocument();
    const kpi = screen.getByText("P&L del giorno", { selector: "div" }).closest("div.rounded-lg") as HTMLElement;
    expect(within(kpi).getByText("-250,50 €")).toHaveClass("loss");
    expect(screen.getByText("Worker attivo (12 s fa)")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Alfa" })).toHaveAttribute("href", "/portafogli/p1");
    expect(screen.getByText("+0,40%", { exact: false })).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "binance: operativo" })).toBeInTheDocument();
    expect(screen.getByText(/Avviso di margine · Alfa/)).toBeInTheDocument();
  });

  it("«Equity totale» counts every portfolio and names those out of the goals", async () => {
    current = {
      ...dashboard,
      total_equity_eur: "13382.02",
      goals_equity_eur: "12345.60",
      excluded_from_goals: [{ id: "p9", name: "Portafoglio Sfida", equity_eur: "1036.42" }],
    };
    renderApp("/");
    expect(await screen.findByText("13.382,02 €")).toBeInTheDocument();
    expect(screen.getByTestId("excluded-from-goals")).toHaveTextContent("di cui fuori dagli obiettivi: Portafoglio Sfida 1.036,42 €");
  });

  it("shows amber and red worker heartbeat states", async () => {
    current = { ...dashboard, worker_heartbeat_age_s: 60 };
    const { unmount } = renderApp("/");
    expect(await screen.findByText("Worker in ritardo (60 s fa)")).toBeInTheDocument();
    unmount();
    current = { ...dashboard, worker_heartbeat_age_s: null };
    renderApp("/");
    expect(await screen.findByText("Worker non raggiungibile")).toBeInTheDocument();
  });

  it("order: «Obiettivi» and «Andamento» first, then open positions and closest signals; no old equity chart", async () => {
    server.use(
      http.get("*/api/goals/progress", () =>
        HttpResponse.json([
          {
            goal_id: "g1", name: "settimanale", horizon: "week", scope: "total", portfolio_id: null, portfolio_name: null, kind: "min",
            period_start: "2026-11-16", period_end: "2026-11-22", start_eur: null, current_eur: null, target_eur: null, pct: null,
            target_pct: "2", gap_eur: null, elapsed_pct: "0", status: "non iniziato", as_of: null,
          } satisfies GoalProgress,
        ]),
      ),
    );
    renderApp("/");
    const goal = await screen.findByRole("list", { name: "Obiettivi attivi" });
    const trend = await screen.findByText("Andamento");
    const positions = await screen.findByText("Posizioni aperte");
    const signalsCard = screen.getByText("Segnali più vicini");
    const kpi = screen.getByText("Equity totale");
    const follows = (a: Node, b: Node) => Boolean(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING);
    expect(follows(goal, trend) && follows(trend, positions) && follows(positions, signalsCard) && follows(signalsCard, kpi)).toBe(true);
    expect(screen.queryByText("Equity aggregata")).not.toBeInTheDocument();
    expect(await screen.findByText("Nessuna posizione aperta")).toBeInTheDocument();
    expect(await screen.findByText("Nessun segnale vicino")).toBeInTheDocument();
  });

  it("compact «Posizioni aperte» and «Segnali più vicini» with links", async () => {
    const position = {
      portfolio_id: "p1", portfolio_name: "Alfa", symbol: "AAPL", name: "Apple", market: "NASDAQ", currency: "USD", side: "long",
      qty: "2", avg_price: "180", opened_at: null, last: "190", stale: false, value_eur: "350", pnl_eur: "18.4", pnl_pct: "5.5",
      stop: null, target: null, distance_to_stop_pct: null, risk_at_stop_eur: null, protection: "senza stop",
    };
    const sym = (symbol: string, distance: string, over = {}) => ({
      symbol, last: "100", indicator: "RSI 14", value_closed: "40", value_now: null, threshold: "30", direction: "below",
      distance, triggered_closed: false, triggered_now: false, as_of: null, position: null, open_entry: false, ...over,
    });
    server.use(
      http.get("*/api/positions/open", () => HttpResponse.json({ as_of: "2026-11-16T12:00:00Z", positions: [position], portfolios: [] })),
      http.get("*/api/signals", () =>
        HttpResponse.json({
          as_of: "2026-11-16T12:00:00Z",
          portfolios: [{
            portfolio_id: "p1", portfolio_name: "Alfa",
            strategies: [{ ref: "STR-1", name: "RSI", type: "rsi_reversion", params: {}, timeframe: "1d", status: "ok",
              symbols: [sym("FAR", "20"), sym("NEAR", "2"), sym("HELD", "1", { position: { qty: "1", avg_price: "1" } }),
                sym("A", "8"), sym("B", "9"), sym("C", "10"), sym("D", "11")] }],
          }],
        }),
      ),
    );
    renderApp("/");
    const positions = await screen.findByRole("list", { name: "Posizioni aperte" });
    expect(within(positions).getByText("AAPL")).toBeInTheDocument();
    expect(within(positions).getByText("+18,40 €")).toHaveClass("text-gain");
    expect(within(positions).getByText("🔴 senza stop")).toBeInTheDocument();
    expect(within(positions).queryByText(/rischio gap/)).toBeNull();
    expect(screen.getByRole("link", { name: "Tutte le posizioni" })).toHaveAttribute("href", "/posizioni");
    const closest = await screen.findByRole("list", { name: "Segnali più vicini" });
    const items = within(closest).getAllByRole("listitem");
    expect(items.map((li) => li.querySelector(".font-medium")?.textContent)).toEqual(["NEAR", "A", "B", "C", "D"]);
    expect(within(items[0]).getByText("🟡 vicino")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Tutti i segnali" })).toHaveAttribute("href", "/segnali");
  });

  it("compact «Posizioni aperte» shows the gap-risk badge only", async () => {
    const position = {
      portfolio_id: "p1", portfolio_name: "Alfa", symbol: "AAPL", name: "Apple", market: "NASDAQ", currency: "USD", side: "short",
      qty: "-2", avg_price: "180", opened_at: null, last: "190", stale: false, value_eur: "-350", pnl_eur: "-18.4", pnl_pct: "-5.5",
      stop: { order_id: "o1", price: "195", tif: "gtc" }, target: null, distance_to_stop_pct: "2.6", risk_at_stop_eur: "9", protection: "ok",
      extended: { session: "pre", price: "199", change_pct_vs_close: "4.7", ts: "2026-11-17T12:00:00Z" }, extended_beyond_stop: true,
    };
    server.use(http.get("*/api/positions/open", () => HttpResponse.json({ as_of: "2026-11-17T12:00:00Z", positions: [position], portfolios: [] })));
    renderApp("/");
    const positions = await screen.findByRole("list", { name: "Posizioni aperte" });
    expect(within(positions).getByText("⚠ sopra lo stop fuori orario: rischio gap all'apertura")).toBeInTheDocument();
    expect(within(positions).queryByText(/fuori orario 199/)).toBeNull(); // the badge only
  });

  it("updates the kill-switch badge from the kill_switch WS event", async () => {
    renderApp("/");
    expect(await screen.findByText("Trading attivo")).toBeInTheDocument();
    emit({ type: "kill_switch", on: true });
    expect(await screen.findByText("Kill-switch attivo")).toBeInTheDocument();
  });

  it("lists order events in the live feed", async () => {
    renderApp("/");
    await screen.findByText("12.345,60 €");
    emit({ type: "order.filled", ts: "2026-11-12T10:00:00Z", symbol: "AAPL", side: "buy", qty: "2", price: "190.5", currency: "USD" });
    emit({ type: "order.canceled", symbol: "MSFT" });
    emit({ type: "quote_noise" });
    const feed = screen.getByRole("list", { name: "Feed ordini" });
    expect(within(feed).getAllByRole("listitem")).toHaveLength(2);
    expect(within(feed).getByText(/AAPL acquisto 2 @ 190,50 \$/)).toBeInTheDocument();
  });

  it("confirms the kill-switch and posts cancel_open", async () => {
    const user = userEvent.setup();
    renderApp("/");
    await user.click(await screen.findByRole("button", { name: "Attiva kill-switch" }));
    await user.click(screen.getByLabelText("Annulla anche gli ordini aperti"));
    await user.click(screen.getByRole("button", { name: "Attiva" }));
    expect(await screen.findByText("Kill-switch attivo")).toBeInTheDocument();
    expect(posted).toEqual({ on: true, cancel_open: true });
    expect(await screen.findByRole("button", { name: "Disattiva kill-switch" })).toBeInTheDocument();
  });

  it("shows a mode badge per portfolio, the count per mode and the banner", async () => {
    server.use(http.get("*/api/dashboard/modes", () => HttpResponse.json(dashboard.modes)));
    renderApp("/");
    const counts = await screen.findByRole("list", { name: "Portafogli per modalità" });
    expect(counts).toHaveTextContent("1 simulato · 1 Alpaca prova · 0 reali");
    expect(screen.getByLabelText(/^Modalità: SMTrading \(simulato\)\./)).toBeInTheDocument();
    expect(screen.getByLabelText(/^Modalità: Alpaca · prova\./)).toBeInTheDocument();
    expect(await screen.findByRole("status", { name: "Modalità di trading" })).toHaveTextContent(
      "SMTrading (simulato) e conti di prova dei broker – nessun denaro reale",
    );
  });

  it("dashboard counts per broker label", async () => {
    current = {
      ...dashboard,
      mode_counts: [
        { label: "SMTrading (simulato)", count: 2, live: false },
        { label: "Alpaca · prova", count: 1, live: false },
      ],
    };
    const { unmount } = renderApp("/");
    const counts = await screen.findByRole("list", { name: "Portafogli per modalità" });
    expect(counts).toHaveTextContent("2 simulati · 1 Alpaca prova · 0 reali");
    current = {
      ...dashboard,
      mode_counts: [
        { label: "Trading 212 · demo", count: 1, live: false },
        { label: "🔴 Alpaca · reale · sospeso", count: 1, live: true },
      ],
    };
    unmount();
    renderApp("/");
    const live = await screen.findByRole("list", { name: "Portafogli per modalità" });
    expect(live).toHaveTextContent("1 Trading 212 demo · 1 🔴 Alpaca reale sospeso");
    expect(live).not.toHaveTextContent("0 reali");
  });

  it("says simulation only when no portfolio has a broker, and flags real money in red", async () => {
    current = { ...dashboard, modes: { simulazione: 2, paper: 0, reale: 0 } };
    server.use(http.get("*/api/dashboard/modes", () => HttpResponse.json(current.modes)));
    const { unmount } = renderApp("/");
    expect(await screen.findByRole("status", { name: "Modalità di trading" })).toHaveTextContent(
      "SMTrading (simulato) – denaro virtuale, prezzi di mercato reali",
    );
    unmount();
    current = { ...dashboard, modes: { simulazione: 1, paper: 0, reale: 1 } };
    renderApp("/");
    const banner = await screen.findByRole("status", { name: "Modalità di trading" });
    expect(banner).toHaveTextContent("denaro reale");
    expect(banner.className).toContain("text-loss");
  });

  it("follows the held symbols and refetches at most once per window while ticks arrive", async () => {
    current = { ...dashboard, held_symbols: ["AAPL", "BTC-USD"] };
    renderApp("/");
    await screen.findByText("12.345,60 €");
    await waitFor(() => expect(live.subscribed).toContainEqual(["AAPL", "BTC-USD"]));
    const before = fetches;
    current = { ...current, total_equity_eur: "13000" };
    for (let i = 0; i < 6; i++) tick("AAPL");
    expect(await screen.findByText("13.000,00 €")).toBeInTheDocument(); // leading refetch
    expect(fetches).toBe(before + 1);
    await waitFor(() => expect(fetches).toBe(before + 2), { timeout: 4500 }); // one trailing
    await act(() => new Promise((r) => setTimeout(r, 300)));
    expect(fetches).toBe(before + 2);
  });

  it("refetches on every relevant event, not on unrelated ones", async () => {
    renderApp("/");
    await screen.findByText("12.345,60 €");
    const before = fetches;
    emit({ type: "something.else" });
    await act(() => new Promise((r) => setTimeout(r, 100)));
    expect(fetches).toBe(before);
    current = { ...dashboard, total_equity_eur: "999" };
    emit({ type: "portfolio.cash", portfolio_id: "p1" });
    expect(await screen.findByText("999,00 €")).toBeInTheDocument();
    expect(fetches).toBe(before + 1);
  });

  it("shows «Live» while connected (even when quiet), «In pausa» only when the WS is down", async () => {
    const { unmount, queryClient } = renderApp("/");
    const indicator = await screen.findByRole("status", { name: "Aggiornamento dashboard" });
    await waitFor(() => expect(indicator).toHaveTextContent(/Live · ultimo aggiornamento \d{2}:\d{2}:\d{2}/));
    // N1: still polled while connected (dead worker, quiet market)
    const query = queryClient.getQueryCache().find({ queryKey: ["dashboard"], exact: true });
    expect(query?.observers[0]?.options.refetchInterval).toBe(45_000);
    // quiet for longer than the freshness window: still «Live», never «In pausa»
    const realNow = Date.now;
    Date.now = () => realNow() + 60_000;
    try {
      await act(() => new Promise((r) => setTimeout(r, 1100)));
      expect(indicator).toHaveTextContent("Live · ultimo aggiornamento");
      expect(indicator).not.toHaveTextContent("In pausa");
    } finally {
      Date.now = realNow;
    }
    unmount();
    live.status = "closed";
    const second = renderApp("/");
    const paused = await screen.findByRole("status", { name: "Aggiornamento dashboard" });
    await waitFor(() => expect(paused).toHaveTextContent("In pausa · ultimo aggiornamento"));
    const polled = second.queryClient.getQueryCache().find({ queryKey: ["dashboard"], exact: true });
    expect(polled?.observers[0]?.options.refetchInterval).toBe(30_000);
  });
});

describe("Dashboard halt banner", () => {
  it("shows the active halt and revokes it after confirmation", async () => {
    const user = userEvent.setup();
    let active = [haltFixture()];
    const cleared: string[] = [];
    server.use(
      http.get("*/api/halts", () => HttpResponse.json({ active, history: [] })),
      http.post("*/api/halts/:id/clear", ({ params }) => {
        cleared.push(String(params.id));
        const halt = { ...active[0], active: false };
        active = [];
        return HttpResponse.json({ halt, changed: true });
      }),
    );
    renderApp("/");
    const banner = await screen.findByRole("alert", { name: "Blocco aperture" });
    expect(banner).toHaveTextContent("Aperture sospese dal CEO — Drawdown oltre il 5% — dal 14/11/2026 10:30:00");
    expect(banner).toHaveTextContent("(impostato da CEO SMTrading)");
    await user.click(within(banner).getByRole("button", { name: "Revoca" }));
    const dialog = await screen.findByRole("dialog", { name: "Revocare il blocco aperture?" });
    await user.click(within(dialog).getByRole("button", { name: "Revoca" }));
    await waitFor(() => expect(cleared).toEqual(["1"]));
    await waitFor(() => expect(screen.queryByRole("alert", { name: "Blocco aperture" })).not.toBeInTheDocument());
  });
});
