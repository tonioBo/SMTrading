import { act, screen, within } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { POSITIONS_REFRESH_MS } from "@/api/hooks/positions";
import type { IntradayAccount, OpenPosition, OpenPositionsResponse } from "@/api/types";
import { renderApp } from "@/test/render";
import { commonHandlers } from "@/test/handlers";
import { resetViewport, setViewportWidth } from "@/test/viewport";

vi.mock("@/lib/ws", async (orig) => ({
  ...(await orig<typeof import("@/lib/ws")>()),
  useLiveSocket: () => ({ socket: null, status: "open" }),
}));

function pos(over: Partial<OpenPosition>): OpenPosition {
  return {
    portfolio_id: "p1", portfolio_name: "Portafoglio Aggressivo", symbol: "AMZN", name: "Amazon.com Inc",
    market: "NASDAQ", currency: "USD", side: "long", qty: "0.51", avg_price: "258", opened_at: "2026-11-16T20:29:22Z",
    last: "262.4", stale: false, value_eur: "115.32", pnl_eur: "1.93", pnl_pct: "1.71",
    stop: { order_id: "o1", price: "242.2", tif: "gtc" }, target: { order_id: "o2", price: "284", tif: "gtc" },
    distance_to_stop_pct: "7.70", risk_at_stop_eur: "7.75", protection: "ok", ...over,
  };
}

const body: OpenPositionsResponse = {
  as_of: "2026-11-16T21:00:00Z",
  positions: [
    pos({}),
    pos({ portfolio_id: "p2", portfolio_name: "Prudente", symbol: "ENI.MI", name: null, currency: "EUR", stop: null, target: null, distance_to_stop_pct: null, risk_at_stop_eur: null, protection: "senza stop" }),
    pos({ portfolio_id: "p2", portfolio_name: "Prudente", symbol: "BTC-USD", stop: { order_id: "o3", price: "90000", tif: "day" }, protection: "scade oggi" }),
    pos({ portfolio_id: "p2", portfolio_name: "Prudente", symbol: "ZZZ", last: null, value_eur: null, pnl_eur: null, pnl_pct: null, distance_to_stop_pct: null, risk_at_stop_eur: null, protection: "copre parziale" }),
  ],
  portfolios: [
    {
      portfolio_id: "p1", portfolio_name: "Portafoglio Aggressivo", mode: "reale", live_state: "armed", mode_label: "eToro · reale",
      positions: 1, value_eur: "115.32", pnl_eur: "1.93", risk_at_stop_eur: "7.75", risk_unknown: 0, unprotected: 0,
      partial_cover: 0, expiring_today: 0, daily_loss_limit_eur: "40", risk_pct_of_limit: "19.38",
    },
    {
      portfolio_id: "p2", portfolio_name: "Prudente", mode: "simulazione", live_state: null, mode_label: "SMTrading (simulato)",
      positions: 3, value_eur: null, pnl_eur: null, risk_at_stop_eur: "0", risk_unknown: 1, unprotected: 1,
      partial_cover: 1, expiring_today: 1, daily_loss_limit_eur: null, risk_pct_of_limit: null,
    },
  ],
};

let current: OpenPositionsResponse = body;
let accounts: IntradayAccount[] = [];
let fetches = 0;
const server = setupServer(
  http.get("*/api/auth/me", () => HttpResponse.json({ email: "v@example.com", role: "viewer", totp_enabled: false })),
  http.get("*/api/kill-switch", () => HttpResponse.json({ on: false })),
  http.get("*/api/intraday/accounts", () => HttpResponse.json(accounts)),
  http.get("*/api/positions/open", () => {
    fetches += 1;
    return HttpResponse.json(current);
  }),
  ...commonHandlers, // last: the handlers above (this page's data) take precedence
);
beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  server.resetHandlers();
  current = body;
  accounts = [];
  fetches = 0;
  vi.useRealTimers();
});
afterAll(() => server.close());

describe("Positions", () => {
  it("groups the positions by portfolio with protection badges and the risk line", async () => {
    renderApp("/posizioni");
    expect(await screen.findByRole("heading", { name: "Posizioni" })).toBeInTheDocument();
    expect(await screen.findByText("Rischio agli stop: 7,75 € su 40,00 € (19%)")).toBeInTheDocument();
    const agg = screen.getByRole("table", { name: "Posizioni Portafoglio Aggressivo" });
    const row = within(agg).getAllByRole("row")[1];
    expect(within(row).getByText("AMZN")).toBeInTheDocument();
    expect(within(row).getByRole("link", { name: "Diario AMZN" })).toHaveAttribute("href", "/diario-operazioni?portfolio_id=p1&symbol=AMZN&outcome=open");
    expect(within(row).getByText("0,51")).toBeInTheDocument();
    expect(within(row).getByText("242,20 $")).toBeInTheDocument();
    expect(within(row).getByText("284,00 $")).toBeInTheDocument();
    expect(within(row).getByText("7,70%")).toBeInTheDocument();
    expect(within(row).getByText("7,75 €")).toBeInTheDocument();
    expect(within(row).getByText("🟢 protetta")).toBeInTheDocument();

    const pru = screen.getByRole("table", { name: "Posizioni Prudente" });
    const rows = within(pru).getAllByRole("row");
    expect(within(rows[1]).getByText("🔴 senza stop")).toBeInTheDocument();
    expect(within(rows[2]).getByText("🟠 scade oggi")).toBeInTheDocument();
    expect(within(rows[2]).getByText("giornaliero")).toBeInTheDocument();
    // no quote: «—», never 0
    expect(within(rows[3]).getAllByText("—").length).toBeGreaterThanOrEqual(5);
    expect(within(rows[3]).queryByText("0,00 €")).not.toBeInTheDocument();
    expect(screen.getByText("Rischio agli stop: 0,00 € (nessun limite di perdita giornaliera)")).toBeInTheDocument();
    expect(screen.getByText("1 senza stop")).toBeInTheDocument();
    expect(within(rows[3]).getByText("🟠 copre parziale")).toBeInTheDocument();
    expect(screen.getByText("1 copre parziale")).toBeInTheDocument();
  });

  it("shows the extended-hours price and the gap-risk badge below the stop", async () => {
    current = {
      ...body,
      positions: [
        pos({ extended: { session: "post", price: "240", change_pct_vs_close: "-8.54", ts: "2026-11-16T21:45:00Z" }, extended_beyond_stop: true }),
        pos({ portfolio_id: "p2", portfolio_name: "Prudente", symbol: "MSFT", extended: { session: "pre", price: "263", change_pct_vs_close: "0.23", ts: "2026-11-17T12:00:00Z" }, extended_beyond_stop: false }),
      ],
    };
    renderApp("/posizioni");
    const agg = await screen.findByRole("table", { name: "Posizioni Portafoglio Aggressivo" });
    const row = within(agg).getAllByRole("row")[1];
    expect(within(row).getByText(/^fuori orario/)).toHaveTextContent("fuori orario 240,00 $ (-8,5%)");
    expect(within(row).getByText("⚠ sotto lo stop fuori orario: rischio gap all'apertura")).toBeInTheDocument();
    expect(within(row).getByText("262,40 $")).toBeInTheDocument(); // the regular price is unchanged
    const pru = screen.getByRole("table", { name: "Posizioni Prudente" });
    const msft = within(pru).getAllByRole("row")[1];
    expect(within(msft).getByText(/^fuori orario/)).toHaveTextContent("fuori orario 263,00 $ (+0,2%)");
    expect(within(msft).queryByText(/rischio gap/)).toBeNull();
  });

  it("shows the «In chiusura (fine piano)» badge, no deadline", async () => {
    current = {
      ...body,
      positions: [
        pos({ plan_state: "closing" }),
        pos({ portfolio_id: "p2", portfolio_name: "Prudente", symbol: "MSFT", plan_state: "pending" }),
        pos({ portfolio_id: "p2", portfolio_name: "Prudente", symbol: "ENI.MI", plan_state: null }),
      ],
    };
    renderApp("/posizioni");
    const agg = await screen.findByRole("table", { name: "Posizioni Portafoglio Aggressivo" });
    expect(within(agg).getByText("In chiusura (fine piano)")).toBeInTheDocument();
    expect(within(agg).queryByText(/fino al/)).toBeNull();
    const pru = screen.getByRole("table", { name: "Posizioni Prudente" });
    const [, msft, eni] = within(pru).getAllByRole("row");
    expect(within(msft).getByText("In chiusura (fine piano)")).toBeInTheDocument();
    expect(within(eni).queryByText(/In chiusura/)).toBeNull();
  });

  it("on a phone every position is a card: symbol first, then labelled values", async () => {
    setViewportWidth(390);
    try {
      renderApp("/posizioni");
      const list = await screen.findByRole("list", { name: "Posizioni Portafoglio Aggressivo" });
      expect(screen.queryByRole("table")).not.toBeInTheDocument();
      const card = within(list).getAllByRole("listitem")[0];
      expect(card.firstElementChild).toHaveTextContent("AMZN");
      expect(within(card).getByText("Rischio allo stop")).toBeInTheDocument();
      expect(within(card).getByText("7,75 €")).toBeInTheDocument();
      expect(within(card).getByText("🟢 protetta")).toBeInTheDocument();
    } finally {
      resetViewport();
    }
  });

  it("shows the empty state", async () => {
    current = { as_of: "2026-11-16T21:00:00Z", positions: [], portfolios: [] };
    renderApp("/posizioni");
    expect(await screen.findByText("Nessuna posizione aperta")).toBeInTheDocument();
  });

  it("refreshes once a minute", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    renderApp("/posizioni");
    expect(await screen.findByText("Rischio agli stop: 7,75 € su 40,00 € (19%)")).toBeInTheDocument();
    expect(fetches).toBe(1);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(POSITIONS_REFRESH_MS - 1000);
    });
    expect(fetches).toBe(1);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000);
    });
    expect(fetches).toBe(2);
  });

  it("is listed in the sidebar after Segnali", async () => {
    renderApp("/posizioni");
    const nav = await screen.findByRole("navigation", { name: "Navigazione principale" });
    const labels = within(nav).getAllByRole("link").map((a) => a.textContent);
    expect(labels.indexOf("Posizioni")).toBe(labels.indexOf("Segnali") + 1);
  });

  describe("lavoro in giornata", () => {
    const portfolioRow = {
      portfolio_id: "p1", portfolio_name: "Portafoglio Aggressivo", intraday_enabled: true, auto_rules_enabled: false,
      leverage: "2", day_leverage: "2", max_day_trades_5d: 3, used: 2, pending: 0, remaining: 1, frees_on: "2026-12-01",
    };
    const account = (over: Partial<IntradayAccount> = {}): IntradayAccount => ({
      link_id: "l1", link_name: "Alpaca prova", kind: "alpaca_paper", buying_power: "25000", multiplier: "2", equity: "12000",
      trading_blocked: false, account_blocked: false, read_at: "2026-11-27T09:30:00Z", fresh: true, margin: true,
      portfolios: [
        portfolioRow,
        { ...portfolioRow, portfolio_id: "p2", portfolio_name: "Prudente", intraday_enabled: false, auto_rules_enabled: true, max_day_trades_5d: null, used: 1, remaining: null, frees_on: null },
      ],
      ...over,
    });

    it("marks the day quantity and shows the account box with the cap usage", async () => {
      current = { ...body, positions: [pos({ day_qty: "40" }), ...body.positions.slice(1)] };
      accounts = [account()];
      renderApp("/posizioni");
      const agg = await screen.findByRole("table", { name: "Posizioni Portafoglio Aggressivo" });
      expect(within(agg).getByText("di giornata 40")).toBeInTheDocument();
      const box = await screen.findByRole("region", { name: "Conto e operazioni di giornata Alpaca prova" });
      expect(box).toHaveTextContent("potere d'acquisto 25.000,00 $ · margine ×2 · letto alle 10:30");
      expect(box).toHaveTextContent("leva disponibile 2×");
      expect(box).not.toHaveTextContent("conto in contanti");
      expect(box).not.toHaveTextContent("bloccato dal broker");
      expect(box).toHaveTextContent("Portafoglio Aggressivo");
      expect(box).toHaveTextContent("leva in giornata 2");
      expect(box).toHaveTextContent("usate 2 su 3 · restano 1 · la prossima si libera il 01/12");
      expect(box).toHaveTextContent("usate 1 (nessun tetto)");
      expect(box).not.toHaveTextContent(/25\.000 \$ di patrimonio|regola USA|PDT/i);
    });

    it("does not mark a position without day quantity", async () => {
      current = { ...body, positions: [pos({ day_qty: "0" }), ...body.positions.slice(1)] };
      renderApp("/posizioni");
      await screen.findByRole("table", { name: "Posizioni Portafoglio Aggressivo" });
      expect(screen.queryByText(/di giornata/)).not.toBeInTheDocument();
    });

    it("flags a blocked account and a stale read, and an account without leverage", async () => {
      accounts = [account({ trading_blocked: true, fresh: false, margin: false, multiplier: "1" })];
      renderApp("/posizioni");
      const box = await screen.findByRole("region", { name: "Conto e operazioni di giornata Alpaca prova" });
      expect(box).toHaveTextContent("bloccato dal broker");
      expect(box).toHaveTextContent("non letto di recente");
      expect(box).toHaveTextContent("senza leva");
      expect(box).not.toHaveTextContent("conto in contanti");
      expect(box).not.toHaveTextContent("leva disponibile");
    });

    it("shows no box for a link without portfolios in day mode", async () => {
      accounts = [account({ portfolios: [{ ...portfolioRow, intraday_enabled: false }] })];
      renderApp("/posizioni");
      await screen.findByRole("table", { name: "Posizioni Portafoglio Aggressivo" });
      expect(screen.queryByRole("region", { name: /Conto e operazioni di giornata/ })).not.toBeInTheDocument();
    });
  });
});
