import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import type { AutoRule, AutoRuleResult, AutoTrade, ModelSpec, ReplayResult } from "@/api/types";
import { commonHandlers } from "@/test/handlers";
import { renderApp } from "@/test/render";

vi.mock("@/lib/ws", async (orig) => ({
  ...(await orig<typeof import("@/lib/ws")>()),
  useLiveSocket: () => ({ socket: null, status: "open" }),
}));

const COMMON = [
  { name: "stop_pct", kind: "dec", lo: "0.05", hi: "5", default: "0.3", choices: [], common: true },
  { name: "target_pct", kind: "dec", lo: "0.05", hi: "10", default: "0.5", choices: [], common: true },
  { name: "max_hold_s", kind: "int", lo: 10, hi: 3600, default: 300, choices: [], common: true },
  { name: "entry", kind: "choice", lo: null, hi: null, default: "market", choices: ["market", "limit"], common: true },
  { name: "max_age_s", kind: "dec", lo: "0.5", hi: "10", default: "2", choices: [], common: true },
];
const MODELS = [
  {
    name: "breakout",
    params: [
      { name: "lookback_min", kind: "int", lo: 5, hi: 240, default: 20, choices: [], common: false },
      { name: "volume_mult", kind: "opt_dec", lo: "1", hi: "10", default: null, choices: [], common: false },
      ...COMMON,
    ],
  },
] as ModelSpec[];

const rule = (over: Partial<AutoRule> = {}): AutoRule => ({
  id: "r1", portfolio_id: "p1", portfolio_name: "Prova", symbol: "AAPL", model: "breakout", status: "draft", status_it: "bozza",
  status_reason: null, status_changed_at: null, version: 1, params: { lookback_min: 20, stop_pct: "0.3" }, qty: "5", notional: null,
  brakes: { max_trades_hour: 20 }, stream: "stream", stream_it: "con flusso", open_trade: false,
  today: { trades: 2, net: "12.5", net_currency: "USD" }, ...over,
});

const trade: AutoTrade = {
  id: 7, rule_id: "r1", rule_version: 1, symbol: "AAPL", trigger_at: "2026-11-26T15:00:00Z", trigger_price: "190.10", expected_price: "190.12",
  status: "closed", filled_at: "2026-11-26T15:00:01Z", fill_price: "190.15", qty: "5", exit_reason: "time", exit_filled_at: "2026-11-26T15:05:00Z",
  exit_expected: "190.40", exit_price: "190.38", fees: "0.02", fees_currency: "USD", net_result: "1.05", net_currency: "USD",
  entry_slippage_bps: "1.5", exit_slippage_bps: "0.5", ms_trigger_to_send: 120, ms_send_to_broker: 45, ms_broker_to_fill: 310, error: null,
};

const result: AutoRuleResult = {
  rule_id: "r1", version: 1, day: "2026-11-26", trades: 3, wins: 2, losses: 1, net: "4.2", net_currency: "USD", fees: { USD: "0.06" },
  median_ms_trigger_to_send: "120", median_ms_send_to_broker: "45", median_ms_broker_to_fill: "310", avg_entry_slippage_bps: "1.50",
  blocked: { spread: 4, stale: 2 },
};

const NOTE = "al minuto non si vedono i movimenti dentro il minuto; volume solo IEX per le azioni";
const replay: ReplayResult = {
  symbols: ["AAPL"], from: "2026-11-19T00:00:00Z", to: "2026-11-26T00:00:00Z", trades_total: 600, truncated: true, net: "9.5", fees: "1.2", wins: 350, losses: 250,
  max_drawdown: "3.1", notes: [NOTE], per_symbol: [],
  trades: [{ symbol: "AAPL", entry_ts: "2026-11-25T15:00:00Z", entry_price: "190", exit_ts: "2026-11-25T15:03:00Z", exit_price: "190.5", exit_reason: "target", qty: "5", fees: "0.02", net: "2.48" }],
};

let role = "admin";
let rules: AutoRule[] = [rule()];
let posts: unknown[] = [];
let calls: string[] = [];
let activateResponse: () => Response = () => HttpResponse.json(rule({ status: "active", status_it: "attiva" }));
let backtestResponse: () => Response = () => HttpResponse.json(replay);
let backtests: unknown[] = [];
const server = setupServer(
  http.get("*/api/auth/me", () => HttpResponse.json({ email: "a@example.com", role, totp_enabled: false })),
  http.get("*/api/dashboard", () => new HttpResponse(null, { status: 503 })),
  http.get("*/api/kill-switch", () => HttpResponse.json({ on: false })),
  http.get("*/api/portfolios", () => HttpResponse.json([{ id: "p1", name: "Prova", base_currency: "USD" }])),
  http.get("*/api/auto-rules/models", () => HttpResponse.json(MODELS)),
  http.get("*/api/auto-rules/results", () => HttpResponse.json([result])),
  http.get("*/api/auto-rules/backtest", () => new HttpResponse(null, { status: 405 })),
  http.get("*/api/auto-rules/:id/trades", () => HttpResponse.json([trade])),
  http.get("*/api/auto-rules", () => {
    calls.push("list");
    return HttpResponse.json(rules);
  }),
  http.post("*/api/auto-rules/backtest", async ({ request }) => {
    backtests.push(await request.json());
    return backtestResponse();
  }),
  http.post("*/api/auto-rules/:id/activate", () => {
    calls.push("activate");
    return activateResponse();
  }),
  http.post("*/api/auto-rules", async ({ request }) => {
    posts.push(await request.json());
    return HttpResponse.json(rule());
  }),
  http.delete("*/api/portfolios/p1/auto-rules", () => {
    calls.push("purge");
    return HttpResponse.json({ rules: 1, trades: 4 });
  }),
  ...commonHandlers,
);
beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  server.resetHandlers();
  role = "admin";
  rules = [rule()];
  posts = [];
  calls = [];
  backtests = [];
  activateResponse = () => HttpResponse.json(rule({ status: "active", status_it: "attiva" }));
  backtestResponse = () => HttpResponse.json(replay);
});
afterAll(() => server.close());

describe("AutoRules", () => {
  it("hides the menu entry from a viewer and does not read the rules for him", async () => {
    role = "viewer";
    renderApp("/");
    await screen.findByRole("navigation", { name: "Navigazione principale" });
    await waitFor(() => expect(screen.queryByRole("link", { name: "Regole automatiche" })).not.toBeInTheDocument());
    renderApp("/regole-automatiche");
    expect(await screen.findByText("Questa pagina è riservata agli amministratori.")).toBeInTheDocument();
    expect(calls).toEqual([]);
  });

  it("shows the menu entry to an admin, with the page's lead text and the rule's state, today figures and price sign", async () => {
    rules = [rule({ stream: "none", stream_it: "senza flusso" })];
    const user = userEvent.setup();
    renderApp("/");
    await user.click(await screen.findByRole("link", { name: "Regole automatiche" }));
    expect(await screen.findByText("Prove: le regole lavorano solo sui portafogli con l'interruttore acceso. Il kill switch ferma tutto.")).toBeInTheDocument();
    const table = await screen.findByRole("table", { name: "Regole automatiche" });
    expect(within(table).getByText("senza flusso")).toBeInTheDocument();
    expect(within(table).getByText("bozza")).toBeInTheDocument();
    expect(within(table).getByText(/Rottura del massimo · versione 1/)).toBeInTheDocument();
    expect(within(table).getByText(/\+12,50/)).toBeInTheDocument();
    expect(within(table).getByText("Oggi (giorno del portafoglio)")).toBeInTheDocument();
  });

  it("shows why a rule was stopped by the brakes and offers Riattiva", async () => {
    rules = [rule({ status: "braked", status_it: "fermata dai freni", status_reason: "3 perdite di fila" })];
    renderApp("/regole-automatiche");
    expect(await screen.findByText("fermata dai freni")).toBeInTheDocument();
    expect(screen.getByText("3 perdite di fila")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Riattiva" })).toBeInTheDocument();
  });

  it("posts the model defaults for a new rule", async () => {
    const user = userEvent.setup();
    renderApp("/regole-automatiche");
    await user.click(await screen.findByRole("button", { name: "Nuova regola" }));
    const form = await screen.findByRole("form", { name: "Nuova regola" });
    await within(within(form).getByLabelText("Portafoglio")).findByRole("option", { name: "Prova" });
    await user.selectOptions(within(form).getByLabelText("Portafoglio"), "p1");
    await user.type(within(form).getByLabelText("Simbolo"), "AAPL");
    await user.type(within(form).getByLabelText("Numero di pezzi"), "5");
    await user.click(within(form).getByRole("button", { name: "Crea la regola" }));
    await waitFor(() => expect(posts).toHaveLength(1));
    expect(posts[0]).toEqual({
      portfolio_id: "p1", symbol: "AAPL", model: "breakout", qty: "5",
      params: { lookback_min: 20, volume_mult: null, stop_pct: "0.3", target_pct: "0.5", max_hold_s: 300, entry: "market", max_age_s: "2" },
      brakes: { max_trades_hour: 20, max_trades_day: 100, max_loss_day: null, max_loss_day_pct: "1", max_consecutive_losses: 3 },
    });
  });

  it("reads a comma decimal as the numeric string and refuses a bad optional value instead of sending null", async () => {
    const user = userEvent.setup();
    renderApp("/regole-automatiche");
    await user.click(await screen.findByRole("button", { name: "Nuova regola" }));
    const form = await screen.findByRole("form", { name: "Nuova regola" });
    await within(within(form).getByLabelText("Portafoglio")).findByRole("option", { name: "Prova" });
    await user.selectOptions(within(form).getByLabelText("Portafoglio"), "p1");
    await user.type(within(form).getByLabelText("Simbolo"), "AAPL");
    await user.type(within(form).getByLabelText("Numero di pezzi"), "5");
    const stop = within(form).getByLabelText("Stop (%)");
    await user.clear(stop);
    await user.type(stop, "0,3");
    const vol = within(form).getByLabelText(/Volume minimo/);
    await user.type(vol, "abc");
    await user.click(within(form).getByRole("button", { name: "Crea la regola" }));
    expect(await within(form).findByText("Volume minimo (multiplo della media): un numero")).toBeInTheDocument();
    expect(posts).toEqual([]);
    await user.clear(vol);
    await user.click(within(form).getByRole("button", { name: "Crea la regola" }));
    await waitFor(() => expect(posts).toHaveLength(1));
    expect((posts[0] as { params: Record<string, unknown> }).params).toMatchObject({ stop_pct: "0.3", volume_mult: null });
  });

  it("refuses a value outside the catalog's bounds in the page, without posting", async () => {
    const user = userEvent.setup();
    renderApp("/regole-automatiche");
    await user.click(await screen.findByRole("button", { name: "Nuova regola" }));
    const form = await screen.findByRole("form", { name: "Nuova regola" });
    await within(within(form).getByLabelText("Portafoglio")).findByRole("option", { name: "Prova" });
    await user.selectOptions(within(form).getByLabelText("Portafoglio"), "p1");
    await user.type(within(form).getByLabelText("Simbolo"), "AAPL");
    await user.type(within(form).getByLabelText("Numero di pezzi"), "5");
    const stop = within(form).getByLabelText("Stop (%)");
    await user.clear(stop);
    await user.type(stop, "9");
    await user.click(within(form).getByRole("button", { name: "Crea la regola" }));
    expect(await within(form).findByText("Stop (%): fuori dai limiti 0,05–5")).toBeInTheDocument();
    expect(posts).toEqual([]);
  });

  it("Attiva calls /activate and shows the server's text on a 400", async () => {
    activateResponse = () => HttpResponse.json({ code: "INVALID_ORDER", message: "regola AAPL: l'interruttore del portafoglio è spento", details: null }, { status: 400 });
    const user = userEvent.setup();
    renderApp("/regole-automatiche");
    await user.click(await screen.findByRole("button", { name: "Attiva" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("regola AAPL: l'interruttore del portafoglio è spento");
    expect(calls).toContain("activate");
  });

  it("shows the results per rule and day, with the blocked entries by reason and the UTC label", async () => {
    const user = userEvent.setup();
    renderApp("/regole-automatiche");
    await user.click(await screen.findByRole("tab", { name: "Risultati" }));
    const table = await screen.findByRole("table", { name: "Risultati per regola e per giorno" });
    expect(within(table).getByText("26/11/2026")).toBeInTheDocument();
    expect(within(table).getByText("forbice larga: 4, prezzo vecchio: 2")).toBeInTheDocument();
    expect(within(table).getByText("2 / 1")).toBeInTheDocument();
    expect(within(table).getByText(/0,06/)).toBeInTheDocument();
    expect(within(table).getByText("Giorno (UTC)")).toBeInTheDocument();
    expect(within(table).getByText("310 ms")).toBeInTheDocument();
  });

  it("shows the exit reason in plain words and the milliseconds in the trades tab", async () => {
    const user = userEvent.setup();
    renderApp("/regole-automatiche");
    await user.click(await screen.findByRole("tab", { name: "Operazioni" }));
    const table = await screen.findByRole("table", { name: "Operazioni della regola" });
    expect(await within(table).findByText("a tempo")).toBeInTheDocument();
    expect(within(table).getByText("120 ms")).toBeInTheDocument();
    expect(within(table).getByText("190,12")).toBeInTheDocument();
    expect(within(table).getByText("190,15")).toBeInTheDocument();
  });

  it("replays on history and shows the minute-limit note as the server gave it, and that the list is cut", async () => {
    const user = userEvent.setup();
    renderApp("/regole-automatiche");
    await user.click(await screen.findByRole("tab", { name: "Prova sullo storico" }));
    await user.click(await screen.findByRole("button", { name: "Avvia la prova" }));
    expect(await screen.findByText(NOTE)).toBeInTheDocument();
    expect(screen.getByText(/Mostrate le ultime 1 operazioni su 600/)).toBeInTheDocument();
    expect(backtests[0]).toMatchObject({ symbol: "AAPL", model: "breakout", qty: "5", spread_pct: "0.05", delay_s: 1, fee_rule: "broker", portfolio_id: "p1" });
    expect(within(await screen.findByRole("table", { name: "Operazioni della prova" })).getByText("obiettivo")).toBeInTheDocument();
  });

  it("shows the server's Italian message when a replay is already running (429)", async () => {
    backtestResponse = () => HttpResponse.json({ code: "RATE_LIMITED", message: "una prova sullo storico è già in corso: riprova tra poco", details: null }, { status: 429 });
    const user = userEvent.setup();
    renderApp("/regole-automatiche");
    await user.click(await screen.findByRole("tab", { name: "Prova sullo storico" }));
    await user.click(await screen.findByRole("button", { name: "Avvia la prova" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("una prova sullo storico è già in corso: riprova tra poco");
  });

  it("deletes a portfolio's rules only after the confirm dialog", async () => {
    const user = userEvent.setup();
    renderApp("/regole-automatiche");
    const pick = await screen.findByLabelText("Portafoglio da svuotare");
    await within(pick).findByRole("option", { name: "Prova" });
    await user.selectOptions(pick, "p1");
    await user.click(screen.getByRole("button", { name: "Cancella le regole del portafoglio" }));
    expect(calls).not.toContain("purge");
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent("Cancellare le regole di Prova?");
    await user.click(within(dialog).getByRole("button", { name: "Cancella tutto" }));
    await waitFor(() => expect(calls).toContain("purge"));
  });
});
