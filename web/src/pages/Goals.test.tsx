import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import type { Goal, GoalProgress, GoalTemplate, PlanState } from "@/api/types";
import { renderApp } from "@/test/render";
import { commonHandlers } from "@/test/handlers";

vi.mock("@/lib/ws", async (orig) => ({
  ...(await orig<typeof import("@/lib/ws")>()),
  useLiveSocket: () => ({ socket: null, status: "open" }),
}));

function goal(id: string, name: string, over: Partial<Goal> = {}): Goal {
  return {
    id, name, horizon: "week", start_date: "2026-01-05", end_date: null, recurring: true, scope: "total",
    portfolio_id: null, target_pct: "2", kind: "min", active: true, created_at: "2026-11-01T10:00:00Z",
    updated_at: "2026-11-01T10:00:00Z", portfolio_name: null, portfolio_archived: false, ...over,
  };
}

function progress(g: Goal, over: Partial<GoalProgress> = {}): GoalProgress {
  return {
    goal_id: g.id, name: g.name, horizon: g.horizon, scope: g.scope, portfolio_id: g.portfolio_id,
    portfolio_name: g.portfolio_name, kind: g.kind, period_start: "2026-11-16", period_end: "2026-11-22",
    start_eur: "1000", current_eur: "1010", target_eur: "1020", pct: "1", target_pct: g.target_pct,
    gap_eur: "10", elapsed_pct: "40", status: "in linea", as_of: "2026-11-17T09:00:00Z", ...over,
  };
}

const weekly = goal("g1", "settimanale");
const xmas = goal("g2", "Natale", {
  horizon: "custom", start_date: "2026-11-01", end_date: "2026-12-24", recurring: false, scope: "portfolio",
  portfolio_id: "p1", portfolio_name: "Alfa", target_pct: "5", kind: "cap",
});
const old = goal("g3", "vecchio", { active: false, horizon: "month" });
const risk = goal("g4", "rischio", {
  horizon: "month", scope: "portfolio", portfolio_id: "p1", portfolio_name: "Alfa", target_pct: "5", kind: "max_loss",
});
const index = goal("g5", "indice", { horizon: "quarter", target_pct: "0", kind: "beat_index", benchmark: "VWCE.DE" });

const templates: GoalTemplate[] = [
  {
    key: "weekly", label: "Settimanale +2% totale", exists: true,
    body: { name: "Settimanale +2%", horizon: "week", recurring: true, scope: "total", portfolio_id: null, target_pct: "2", kind: "min" },
  },
  {
    key: "risk-p2", label: "Rischio mensile −5% · Beta",
    body: {
      name: "Rischio mensile Beta", horizon: "month", recurring: true, scope: "portfolio", portfolio_id: "p2",
      target_pct: "5", kind: "max_loss", benchmark: null,
    },
  },
  {
    key: "vwce", label: "Battere VWCE nel trimestre (totale)",
    body: {
      name: "Battere VWCE", horizon: "quarter", recurring: true, scope: "total", portfolio_id: null,
      target_pct: "0", kind: "beat_index", benchmark: "VWCE.DE",
    },
  },
];
let templateCalls = 0;
let plans: PlanState[] = [];

let role: "admin" | "viewer" = "admin";
let goals: Goal[] = [];
let calls: { method: string; path: string; body?: unknown }[] = [];
let postError: { field: string; message: string } | null = null;

const server = setupServer(
  // before commonHandlers (they answer the progress with an empty list)
  http.get("*/api/goals/progress", () =>
    HttpResponse.json([
      progress(weekly, {
        parts: [
          { portfolio_id: "p1", name: "Alfa", change_eur: "7.50", contribution_pct: "0.75" },
          { portfolio_id: "p2", name: "Beta", change_eur: "2.50", contribution_pct: "0.25" },
        ],
      }),
      progress(xmas, {
        period_start: "2026-11-01", period_end: "2026-12-24", start_eur: "600", current_eur: "636", target_eur: "630",
        pct: "6", gap_eur: "-6", elapsed_pct: "30", status: "oltre il tetto", portfolio_name: "Alfa",
      }),
      progress(risk, {
        period_start: "2026-11-01", period_end: "2026-11-30", start_eur: "600", current_eur: "576", target_eur: "570",
        pct: "-4", gap_eur: "6", elapsed_pct: "55", status: "vicino al limite", loss_pct: "4", portfolio_name: "Alfa",
      }),
      progress(index, {
        period_start: "2026-10-01", period_end: "2026-12-31", target_eur: null, pct: "3", gap_eur: null, elapsed_pct: "52",
        status: "avanti", benchmark: "VWCE.DE", benchmark_pct: "2", diff_pp: "1",
      }),
    ]),
  ),
  http.get("*/api/goals/plans", () => HttpResponse.json(plans)),
  http.get("*/api/goals/templates", () => {
    templateCalls += 1;
    return HttpResponse.json(templates);
  }),
  ...commonHandlers,
  http.get("*/api/auth/me", () => HttpResponse.json({ email: "a@example.com", role, totp_enabled: false })),
  http.get("*/api/kill-switch", () => HttpResponse.json({ on: false })),
  http.get("*/api/portfolios", () =>
    HttpResponse.json([
      { id: "p1", name: "Alfa", status: "active" },
      { id: "p2", name: "Beta", status: "active" },
    ]),
  ),
  http.get("*/api/goals", () => HttpResponse.json(goals)),
  http.post("*/api/goals", async ({ request }) => {
    const body = await request.json();
    calls.push({ method: "POST", path: "/api/goals", body });
    if (postError) {
      return HttpResponse.json(
        { code: "INVALID_ORDER", message: postError.message, details: { field: postError.field } },
        { status: 400 },
      );
    }
    return HttpResponse.json(goal("g9", "nuovo"));
  }),
  http.patch("*/api/goals/:id", async ({ request, params }) => {
    calls.push({ method: "PATCH", path: `/api/goals/${String(params.id)}`, body: await request.json() });
    return HttpResponse.json(goals.find((g) => g.id === params.id));
  }),
  http.delete("*/api/goals/:id", ({ params }) => {
    calls.push({ method: "DELETE", path: `/api/goals/${String(params.id)}` });
    return HttpResponse.json({ ...goals.find((g) => g.id === params.id), active: false });
  }),
);
beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  server.resetHandlers();
  role = "admin";
  calls = [];
  postError = null;
  templateCalls = 0;
  plans = [];
});
afterAll(() => server.close());

function reset() {
  goals = [weekly, xmas, old, risk, index];
}

describe("Obiettivi", () => {
  it("is in the Trading menu right after «Strategie»", async () => {
    reset();
    renderApp("/obiettivi");
    const nav = await screen.findByRole("navigation", { name: "Navigazione principale" });
    const trading = within(nav).getAllByRole("group")[0];
    const links = within(trading).getAllByRole("link").map((a) => a.textContent);
    expect(links.indexOf("Obiettivi")).toBe(links.indexOf("Strategie") + 1);
    expect(within(trading).getByRole("link", { name: "Obiettivi" })).toHaveAttribute("href", "/obiettivi");
  });

  it("lists the active goals with progress, status and figures, then the inactive ones", async () => {
    reset();
    renderApp("/obiettivi");
    expect(await screen.findByRole("heading", { name: "Obiettivi" })).toBeInTheDocument();
    const w = await screen.findByRole("article", { name: "Obiettivo settimanale" });
    expect(await within(w).findByText("in linea")).toBeInTheDocument();
    expect(within(w).getByText("+2%")).toBeInTheDocument();
    expect(within(w).getByText(/Totale · ogni settimana · 16\/11\/2026 – 22\/11\/2026/)).toBeInTheDocument();
    expect(within(w).getByText("1.000,00 € → 1.010,00 €")).toBeInTheDocument();
    expect(within(w).getByText("+1,00%")).toBeInTheDocument();
    expect(within(w).getByText(/mancano 10,00 €/)).toBeInTheDocument();
    expect(within(w).getByRole("progressbar", { name: "Avanzamento settimanale" })).toHaveAttribute("aria-valuenow", "50");
    expect(within(w).getByTestId("goal-parts-settimanale")).toHaveTextContent("Alfa +7,50 € (+0,75 pt)· Beta +2,50 € (+0,25 pt)");

    const x = screen.getByRole("article", { name: "Obiettivo Natale" });
    expect(within(x).getByText("oltre il tetto")).toBeInTheDocument();
    expect(within(x).getByText("tetto +5%")).toBeInTheDocument();
    expect(within(x).getByText(/Alfa · dal 01\/11\/2026 al 24\/12\/2026/)).toBeInTheDocument();
    expect(within(x).getByText(/oltre di 6,00 €/)).toBeInTheDocument();

    const inactive = screen.getByRole("table", { name: "Obiettivi disattivati" });
    expect(within(inactive).getByText("vecchio")).toBeInTheDocument();
  });

  it("creates a custom goal on a portfolio (dates, no recurrence) with an it-IT percent", async () => {
    reset();
    const user = userEvent.setup();
    renderApp("/obiettivi");
    await user.click(await screen.findByRole("button", { name: "Nuovo obiettivo" }));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByLabelText("Nome"), "Natale");
    expect(within(dialog).getByLabelText("Ripeti ogni periodo")).toBeInTheDocument();
    await user.selectOptions(within(dialog).getByLabelText("Orizzonte"), "custom");
    expect(within(dialog).queryByLabelText("Ripeti ogni periodo")).not.toBeInTheDocument();
    fireEvent.change(within(dialog).getByLabelText("Dal"), { target: { value: "2026-11-20" } });
    fireEvent.change(within(dialog).getByLabelText("Al"), { target: { value: "2026-12-24" } });
    expect(within(dialog).queryByLabelText("Portafoglio")).not.toBeInTheDocument();
    await user.selectOptions(within(dialog).getByLabelText("Ambito"), "portfolio");
    await user.selectOptions(await within(dialog).findByLabelText("Portafoglio"), "p2");
    await user.clear(within(dialog).getByLabelText("Obiettivo (%)"));
    await user.type(within(dialog).getByLabelText("Obiettivo (%)"), "3,5");
    await user.selectOptions(within(dialog).getByLabelText("Tipo"), "cap");
    await user.click(within(dialog).getByRole("button", { name: "Crea" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(calls).toEqual([
      {
        method: "POST", path: "/api/goals",
        body: {
          name: "Natale", horizon: "custom", start_date: "2026-11-20", end_date: "2026-12-24", recurring: false,
          scope: "portfolio", portfolio_id: "p2", target_pct: "3.5", kind: "cap", benchmark: null, closes_positions: true,
        },
      },
    ]);
  });

  it("shows the risk and index goals with their statuses and figures", async () => {
    reset();
    renderApp("/obiettivi");
    const r = await screen.findByRole("article", { name: "Obiettivo rischio" });
    expect(await within(r).findByText("vicino al limite")).toBeInTheDocument();
    expect(within(r).getByText("vicino al limite")).toHaveClass("text-warn");
    expect(within(r).getByText("perdita max 5%")).toBeInTheDocument();
    expect(within(r).getByText(/perdita 4,00% su un limite di 5%/)).toBeInTheDocument();
    expect(within(r).getByText(/soglia minima 570,00 €/)).toBeInTheDocument();
    expect(within(r).getByRole("progressbar", { name: "Perdita rischio" })).toHaveAttribute("aria-valuenow", "80");

    const i = screen.getByRole("article", { name: "Obiettivo indice" });
    expect(within(i).getByText("avanti")).toHaveClass("text-gain");
    expect(within(i).getByText("meglio di VWCE")).toBeInTheDocument();
    expect(within(i).getByText("+3,00%")).toBeInTheDocument();
    expect(within(i).getByText("VWCE +2,00% · differenza +1,00 pt")).toBeInTheDocument();
    expect(within(i).queryByRole("progressbar")).not.toBeInTheDocument();
  });

  it("creates a risk goal: the target is the loss limit", async () => {
    reset();
    const user = userEvent.setup();
    renderApp("/obiettivi");
    await user.click(await screen.findByRole("button", { name: "Nuovo obiettivo" }));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByLabelText("Nome"), "rischio");
    await user.selectOptions(within(dialog).getByLabelText("Orizzonte"), "month");
    await user.selectOptions(within(dialog).getByLabelText("Tipo"), "max_loss");
    expect(within(dialog).queryByLabelText("Obiettivo (%)")).not.toBeInTheDocument();
    expect(within(dialog).queryByLabelText("Indice")).not.toBeInTheDocument();
    await user.clear(within(dialog).getByLabelText("Limite di perdita (%)"));
    await user.type(within(dialog).getByLabelText("Limite di perdita (%)"), "5");
    await user.click(within(dialog).getByRole("button", { name: "Crea" }));
    await waitFor(() => expect(calls).toHaveLength(1));
    expect(calls[0].body).toMatchObject({ kind: "max_loss", target_pct: "5", benchmark: null, horizon: "month" });
  });

  it("creates an index goal: benchmark select, margin in points defaulting to 0", async () => {
    reset();
    const user = userEvent.setup();
    renderApp("/obiettivi");
    await user.click(await screen.findByRole("button", { name: "Nuovo obiettivo" }));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByLabelText("Nome"), "indice");
    await user.selectOptions(within(dialog).getByLabelText("Tipo"), "beat_index");
    const bench = within(dialog).getByLabelText("Indice");
    expect(within(bench).getAllByRole("option").map((o) => o.textContent)).toEqual([
      "SPY (S&P 500)", "QQQ (Nasdaq 100)", "VWCE (ETF mondiale)",
    ]);
    expect(bench).toHaveValue("VWCE.DE");
    expect(within(dialog).getByLabelText("Margine (punti %)")).toHaveValue("0");
    await user.click(within(dialog).getByRole("button", { name: "Crea" }));
    await waitFor(() => expect(calls).toHaveLength(1));
    expect(calls[0].body).toMatchObject({ kind: "beat_index", target_pct: "0", benchmark: "VWCE.DE" });

    // another index and a margin
    await user.click(screen.getByRole("button", { name: "Nuovo obiettivo" }));
    const again = await screen.findByRole("dialog");
    await user.type(within(again).getByLabelText("Nome"), "spy");
    await user.selectOptions(within(again).getByLabelText("Tipo"), "beat_index");
    await user.selectOptions(within(again).getByLabelText("Indice"), "SPY");
    await user.clear(within(again).getByLabelText("Margine (punti %)"));
    await user.type(within(again).getByLabelText("Margine (punti %)"), "1,5");
    await user.click(within(again).getByRole("button", { name: "Crea" }));
    await waitFor(() => expect(calls).toHaveLength(2));
    expect(calls[1].body).toMatchObject({ kind: "beat_index", target_pct: "1.5", benchmark: "SPY" });
  });

  it("«Modelli»: lists the templates, one prefills the dialog, the admin confirms", async () => {
    reset();
    const user = userEvent.setup();
    renderApp("/obiettivi");
    await user.click(await screen.findByRole("button", { name: "Modelli" }));
    const list = await screen.findByRole("dialog");
    const weekly = await within(list).findByRole("button", { name: "Settimanale +2% totale" });
    expect(weekly).toBeDisabled();
    expect(within(weekly).getByText("già presente")).toBeInTheDocument();
    expect(within(list).getByRole("button", { name: "Rischio mensile −5% · Beta" })).toBeEnabled();
    expect(within(list).getByRole("button", { name: "Battere VWCE nel trimestre (totale)" })).toBeInTheDocument();
    await user.click(within(list).getByRole("button", { name: "Rischio mensile −5% · Beta" }));
    const dialog = await screen.findByRole("dialog", { name: "Nuovo obiettivo" });
    expect(within(dialog).getByLabelText("Nome")).toHaveValue("Rischio mensile Beta");
    expect(within(dialog).getByLabelText("Orizzonte")).toHaveValue("month");
    expect(within(dialog).getByLabelText("Tipo")).toHaveValue("max_loss");
    expect(within(dialog).getByLabelText("Portafoglio")).toHaveValue("p2");
    expect(within(dialog).getByLabelText("Limite di perdita (%)")).toHaveValue("5");
    expect(calls).toEqual([]);
    await user.click(within(dialog).getByRole("button", { name: "Crea" }));
    await waitFor(() => expect(calls).toHaveLength(1));
    expect(calls[0].body).toMatchObject({
      name: "Rischio mensile Beta", horizon: "month", recurring: true, scope: "portfolio", portfolio_id: "p2",
      target_pct: "5", kind: "max_loss", benchmark: null,
    });

    await user.click(screen.getByRole("button", { name: "Modelli" }));
    await user.click(await screen.findByRole("button", { name: "Battere VWCE nel trimestre (totale)" }));
    const idx = await screen.findByRole("dialog", { name: "Nuovo obiettivo" });
    expect(within(idx).getByLabelText("Indice")).toHaveValue("VWCE.DE");
    expect(within(idx).getByLabelText("Margine (punti %)")).toHaveValue("0");
  });

  it("checks the form locally and shows the server error on its field", async () => {
    reset();
    const user = userEvent.setup();
    renderApp("/obiettivi");
    await user.click(await screen.findByRole("button", { name: "Nuovo obiettivo" }));
    const dialog = await screen.findByRole("dialog");
    await user.clear(within(dialog).getByLabelText("Obiettivo (%)"));
    await user.click(within(dialog).getByRole("button", { name: "Crea" }));
    expect(within(dialog).getByText("Il nome è obbligatorio")).toBeInTheDocument();
    expect(within(dialog).getByText("Percentuale non valida")).toBeInTheDocument();
    await user.selectOptions(within(dialog).getByLabelText("Ambito"), "portfolio");
    await user.type(within(dialog).getByLabelText("Nome"), "mensile");
    await user.type(within(dialog).getByLabelText("Obiettivo (%)"), "2");
    await user.click(within(dialog).getByRole("button", { name: "Crea" }));
    expect(within(dialog).getByText("Scegli un portafoglio")).toBeInTheDocument();
    expect(calls).toEqual([]);

    await user.selectOptions(within(dialog).getByLabelText("Ambito"), "total");
    postError = { field: "target_pct", message: "al massimo 2 decimali" };
    await user.click(within(dialog).getByRole("button", { name: "Crea" }));
    expect(await within(dialog).findByText("al massimo 2 decimali")).toBeInTheDocument();
    expect(calls).toHaveLength(1);
  });

  it("a piano: the checkbox with its help, the «Piano» badge, the error on its field and the outcome", async () => {
    reset();
    goals = [{ ...weekly, closes_positions: true }, xmas, old, risk, index];
    plans = [
      {
        goal_id: "g1", period_start: "2026-11-09", period_end: "2026-11-15",
        counts: { pending: 1, closing: 0, closed: 2 },
        positions: [
          { portfolio_id: "p1", portfolio_name: "Alfa", symbol: "AAPL", state: "pending", closed_by: null, closed_at: null },
          { portfolio_id: "p1", portfolio_name: "Alfa", symbol: "BTC-USD", state: "closed", closed_by: "system", closed_at: "2026-11-15T23:05:00Z" },
          { portfolio_id: "p2", portfolio_name: "Beta", symbol: "ENI.MI", state: "closed", closed_by: "agent", closed_at: "2026-11-16T09:00:00Z" },
        ],
      },
    ];
    const user = userEvent.setup();
    renderApp("/obiettivi");
    const w = await screen.findByRole("article", { name: "Obiettivo settimanale" });
    expect(within(w).getByText("Piano")).toBeInTheDocument();
    const outcome = await within(w).findByLabelText("Esito del piano");
    expect(outcome).toHaveTextContent("Fine periodo 09/11/2026 – 15/11/2026: 2 chiuse · 1 in chiusura");
    expect(outcome).toHaveTextContent("AAPL · Alfa: in attesa dell'apertura");
    expect(outcome).not.toHaveTextContent(/grazia|fino al/);
    expect(outcome).toHaveTextContent("BTC-USD · Alfa: chiusa da SMTrading");
    expect(outcome).toHaveTextContent("ENI.MI · Beta: chiusa dagli agenti");
    expect(within(screen.getByRole("article", { name: "Obiettivo Natale" })).queryByText("Piano")).toBeNull();

    // the weekly piano on the total covers every portfolio: a new goal cannot be one
    await user.click(screen.getByRole("button", { name: "Nuovo obiettivo" }));
    const dialog = await screen.findByRole("dialog");
    const box = within(dialog).getByLabelText("Chiude le posizioni a fine periodo");
    expect(box).not.toBeChecked();
    expect(box).toBeDisabled();
    expect(box).toHaveAccessibleDescription(/al massimo un piano per portafoglio/i);
    expect(within(dialog).getByText("Non può essere un piano: «settimanale» chiude già le posizioni di questi portafogli.")).toBeInTheDocument();
    await user.type(within(dialog).getByLabelText("Nome"), "mese");
    await user.click(within(dialog).getByRole("button", { name: "Crea" }));
    await waitFor(() => expect(calls).toHaveLength(1));
    expect(calls[0].body).toMatchObject({ name: "mese", closes_positions: false });
  });

  it("a new goal is a piano by default; unchecking it and the server error on its field", async () => {
    reset();
    const user = userEvent.setup();
    renderApp("/obiettivi");
    await user.click(await screen.findByRole("button", { name: "Nuovo obiettivo" }));
    const dialog = await screen.findByRole("dialog");
    const box = within(dialog).getByLabelText("Chiude le posizioni a fine periodo");
    expect(box).toBeChecked();
    expect(box).toBeEnabled();
    await user.type(within(dialog).getByLabelText("Nome"), "mese");
    postError = { field: "closes_positions", message: "c'è già un piano attivo (altro)" };
    await user.click(within(dialog).getByRole("button", { name: "Crea" }));
    expect(await within(dialog).findByText("c'è già un piano attivo (altro)")).toBeInTheDocument();
    expect(calls[0].body).toMatchObject({ closes_positions: true });
    await user.click(box);
    postError = null;
    await user.click(within(dialog).getByRole("button", { name: "Crea" }));
    await waitFor(() => expect(calls).toHaveLength(2));
    expect(calls[1].body).toMatchObject({ closes_positions: false });
  });

  it("edits a goal with its current values", async () => {
    reset();
    const user = userEvent.setup();
    renderApp("/obiettivi");
    await user.click(await screen.findByRole("button", { name: "Modifica settimanale" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByLabelText("Nome")).toHaveValue("settimanale");
    expect(within(dialog).getByLabelText("Ripeti ogni periodo")).toBeChecked();
    expect(within(dialog).getByLabelText("Obiettivo (%)")).toHaveValue("2");
    await user.clear(within(dialog).getByLabelText("Obiettivo (%)"));
    await user.type(within(dialog).getByLabelText("Obiettivo (%)"), "2,5");
    await user.click(within(dialog).getByRole("button", { name: "Salva" }));
    await waitFor(() => expect(calls).toHaveLength(1));
    expect(calls[0]).toEqual({
      method: "PATCH", path: "/api/goals/g1",
      body: {
        name: "settimanale", horizon: "week", start_date: "2026-01-05", end_date: null, recurring: true,
        scope: "total", portfolio_id: null, target_pct: "2.5", kind: "min", benchmark: null, closes_positions: false,
      },
    });
  });

  it("deactivates after confirmation and reactivates an inactive goal", async () => {
    reset();
    const user = userEvent.setup();
    renderApp("/obiettivi");
    await user.click(await screen.findByRole("button", { name: "Disattiva Natale" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("Disattivare l'obiettivo Natale?")).toBeInTheDocument();
    expect(calls).toEqual([]);
    await user.click(within(dialog).getByRole("button", { name: "Disattiva" }));
    await waitFor(() => expect(calls).toEqual([{ method: "DELETE", path: "/api/goals/g2" }]));
    await user.click(screen.getByRole("button", { name: "Riattiva vecchio" }));
    await waitFor(() => expect(calls[1]).toEqual({ method: "PATCH", path: "/api/goals/g3", body: { active: true } }));
  });

  it("viewer: read-only, no create, edit or deactivate", async () => {
    reset();
    role = "viewer";
    renderApp("/obiettivi");
    expect(await screen.findByRole("article", { name: "Obiettivo settimanale" })).toBeInTheDocument();
    expect(await screen.findByText("vecchio")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Nuovo obiettivo" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Modelli" })).not.toBeInTheDocument();
    expect(templateCalls).toBe(0);
    expect(screen.queryByRole("button", { name: /^Modifica / })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Disattiva / })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Riattiva / })).not.toBeInTheDocument();
  });

  it("empty: invites to create the first goal", async () => {
    goals = [];
    server.use(http.get("*/api/goals/progress", () => HttpResponse.json([])));
    renderApp("/obiettivi");
    expect(await screen.findByText("Nessun obiettivo attivo")).toBeInTheDocument();
  });
});
