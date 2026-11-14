import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import type { Agent, Decision, OutboxItem, TelegramSettings } from "@/api/types";
import type { QueryClient } from "@tanstack/react-query";
import { renderApp } from "@/test/render";
import { commonHandlers } from "@/test/handlers";

vi.mock("@/lib/ws", async (orig) => ({
  ...(await orig<typeof import("@/lib/ws")>()),
  useLiveSocket: () => ({ socket: null, status: "open" }),
}));

const TOKEN = "123456789:AAFsecret_token_value_wxyz";

const baseSettings: TelegramSettings = {
  configured: true,
  enabled: true,
  paused: false,
  bot_token_masked: "••••",
  bot_username: "smt_notifier_bot",
  chat_id: "-1001234567890",
  chat_title: "Sala operativa",
  error: null,
  outbox: { pending: 2, paused: 0, failed: 1 },
};

const outboxItem = (over: Partial<OutboxItem>): OutboxItem => ({
  id: 1,
  source: "system",
  agent: null,
  category: "event",
  level: "executed",
  text: "🟢 Ordine eseguito: BUY 3 AAPL",
  status: "sent",
  attempts: 1,
  last_error: null,
  created_at: "2026-11-13T09:00:00Z",
  sent_at: "2026-11-13T09:00:02Z",
  ...over,
});

const decision = (over: Partial<Decision>): Decision => ({
  id: 10,
  agent: "Notifier",
  agent_id: "a1",
  portfolio: "Alfa",
  portfolio_id: "p1",
  kind: "proposal",
  summary: "Comprare AAPL",
  symbols: ["AAPL"],
  details: {},
  created_at: "2026-11-13T08:00:00Z",
  ...over,
});

const agent: Agent = {
  id: "a1",
  name: "Notifier",
  active: true,
  created_at: "2026-11-01T10:00:00Z",
  last_seen_at: null,
  can_notify: false,
  can_halt: false,
  portfolios: [],
};

let settings: TelegramSettings = baseSettings;
let configPuts: Record<string, unknown>[] = [];
let pausedPuts: unknown[] = [];
let retries: string[] = [];
let retryAll = 0;
let decisionUrls: string[] = [];
let outboxUrls: string[] = [];
let agentPatches: unknown[] = [];
let tests = 0;
let detectBodies: unknown[] = [];

const server = setupServer(
  ...commonHandlers,
  http.get("*/api/auth/me", () => HttpResponse.json({ email: "admin@example.com", totp_enabled: false })),
  http.get("*/api/kill-switch", () => HttpResponse.json({ on: false })),
  http.get("*/api/agents", () => HttpResponse.json([agent])),
  http.get("*/api/portfolios", () => HttpResponse.json([{ id: "p1", name: "Alfa" }])),
  http.get("*/api/telegram", () => HttpResponse.json(settings)),
  http.put("*/api/telegram/config", async ({ request }) => {
    const body = (await request.json()) as Record<string, unknown>;
    configPuts.push(body);
    // an @username is normalised to the numeric id, like the real API does
    const chat = String(body.chat_id).startsWith("@") ? "-100777" : String(body.chat_id);
    settings = { ...settings, ...("chat_id" in body ? { chat_id: chat, chat_title: "Nuovo gruppo" } : {}) };
    return HttpResponse.json(settings);
  }),
  http.post("*/api/telegram/detect-chats", async ({ request }) => {
    detectBodies.push(await request.json());
    return HttpResponse.json({
      chats: [
        { id: -1009876543210, type: "supergroup", title: "Trading desk" },
        { id: -4001, type: "group", title: "Prove" },
      ],
    });
  }),
  http.post("*/api/telegram/test", () => {
    tests += 1;
    return HttpResponse.json({ ok: true });
  }),
  http.put("*/api/telegram/paused", async ({ request }) => {
    const body = (await request.json()) as { paused: boolean };
    pausedPuts.push(body);
    settings = { ...settings, paused: body.paused };
    return HttpResponse.json(settings);
  }),
  http.get("*/api/telegram/outbox", ({ request }) => {
    outboxUrls.push(request.url);
    return HttpResponse.json({
      items: [
        outboxItem({ id: 3, status: "failed", attempts: 10, last_error: "Telegram non raggiungibile", sent_at: null }),
        outboxItem({ id: 2, source: "agent:a1", agent: "Notifier", category: "agent", level: "info", text: "Riepilogo", status: "pending", sent_at: null }),
        outboxItem({ id: 1 }),
      ],
    });
  }),
  http.post("*/api/telegram/outbox/retry-failed", () => {
    retryAll += 1;
    settings = { ...settings, outbox: { ...settings.outbox, failed: 0 } };
    return HttpResponse.json({ retried: 1 });
  }),
  http.post("*/api/telegram/outbox/:id/retry", ({ params }) => {
    retries.push(String(params.id));
    return HttpResponse.json(outboxItem({ id: Number(params.id), status: "pending", attempts: 0 }));
  }),
  http.get("*/api/decisions", ({ request }) => {
    decisionUrls.push(request.url);
    const kind = new URL(request.url).searchParams.get("kind");
    const items = [decision({}), decision({ id: 9, kind: "veto", summary: "Veto su TSLA", symbols: ["TSLA"] })];
    return HttpResponse.json({ items: kind ? items.filter((d) => d.kind === kind) : items });
  }),
  http.patch("*/api/agents/a1", async ({ request }) => {
    agentPatches.push(await request.json());
    return HttpResponse.json({ ...agent, can_notify: true });
  }),
  http.get("*/api/agents/a1/watchlists", () => HttpResponse.json([])),
  http.get("*/api/agents/a1/alerts", () => HttpResponse.json([])),
);
beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  server.resetHandlers();
  settings = baseSettings;
  configPuts = [];
  pausedPuts = [];
  retries = [];
  retryAll = 0;
  decisionUrls = [];
  outboxUrls = [];
  agentPatches = [];
  tests = 0;
  detectBodies = [];
});
afterAll(() => server.close());

function mutationsHoldingToken(queryClient: QueryClient) {
  return queryClient
    .getMutationCache()
    .getAll()
    .filter((m) => JSON.stringify([m.state.variables, m.state.data, m.options.mutationKey]).includes(TOKEN));
}

async function connection() {
  return within(await screen.findByRole("region", { name: "Collegamento" }));
}

describe("Notifiche", () => {
  it("renders masked token and bot name, never the token", async () => {
    const { container } = renderApp("/notifiche");
    const c = await connection();
    expect(c.getByLabelText("Token del bot")).toHaveValue("••••");
    expect(c.getByText("@smt_notifier_bot")).toBeInTheDocument();
    expect(c.getByLabelText("ID del gruppo")).toHaveValue("-1001234567890");
    expect(c.getByText("Sala operativa")).toBeInTheDocument();
    expect(c.getByText(/\/start@smt_notifier_bot/)).toBeInTheDocument();
    expect(container.innerHTML).not.toContain("secret_token");
    expect(screen.getByRole("link", { name: "Notifiche" })).toHaveAttribute("href", "/notifiche");
  });

  it("replace token sends bot_token only when typed", async () => {
    const user = userEvent.setup();
    renderApp("/notifiche");
    const c = await connection();
    // only the chat id changed: no token in the body
    const chat = c.getByLabelText("ID del gruppo");
    await user.clear(chat);
    await user.type(chat, "-4001");
    await user.click(c.getByRole("button", { name: "Salva" }));
    await waitFor(() => expect(configPuts).toEqual([{ chat_id: "-4001" }]));

    await user.click(c.getByRole("button", { name: "Sostituisci token" }));
    const field = c.getByLabelText("Token del bot");
    expect(field).toHaveValue("");
    expect(field).toHaveAttribute("type", "password");
    await user.type(field, `  ${TOKEN} `);
    await user.click(c.getByRole("button", { name: "Salva" }));
    await waitFor(() => expect(configPuts[1]).toEqual({ bot_token: TOKEN }));
    // back to the masked view, the typed token is gone
    await waitFor(() => expect(c.getByLabelText("Token del bot")).toHaveValue("••••"));
    expect(document.body.innerHTML).not.toContain(TOKEN);
  });

  it("unreadable config requires the token again", async () => {
    settings = { ...baseSettings, error: "configurazione illeggibile: reinserisci il token" };
    const user = userEvent.setup();
    renderApp("/notifiche");
    const c = await connection();
    expect(c.getByText("configurazione illeggibile: reinserisci il token")).toBeInTheDocument();
    const field = c.getByLabelText("Token del bot");
    expect(field).toHaveValue("");
    expect(c.queryByRole("button", { name: "Sostituisci token" })).not.toBeInTheDocument();
    await user.click(c.getByRole("button", { name: "Salva" }));
    expect(await c.findByText("Reinserisci il token del bot")).toBeInTheDocument();
    expect(configPuts).toEqual([]);
  });

  it("detect groups fills chat id on click", async () => {
    const user = userEvent.setup();
    renderApp("/notifiche");
    const c = await connection();
    await user.click(c.getByRole("button", { name: "Rileva gruppi" }));
    const list = await c.findByRole("list", { name: "Gruppi rilevati" });
    expect(detectBodies).toEqual([{}]);
    await user.click(within(list).getByRole("button", { name: /Trading desk/ }));
    expect(c.getByLabelText("ID del gruppo")).toHaveValue("-1009876543210");
    expect(c.queryByRole("list", { name: "Gruppi rilevati" })).not.toBeInTheDocument();
  });

  it("detect groups uses the token being typed", async () => {
    const user = userEvent.setup();
    renderApp("/notifiche");
    const c = await connection();
    await user.click(c.getByRole("button", { name: "Sostituisci token" }));
    await user.type(c.getByLabelText("Token del bot"), TOKEN);
    await user.click(c.getByRole("button", { name: "Rileva gruppi" }));
    await waitFor(() => expect(detectBodies).toEqual([{ bot_token: TOKEN }]));
  });

  it("no mutation keeps the token after detect, manual id and save", async () => {
    const user = userEvent.setup();
    const { queryClient } = renderApp("/notifiche");
    const c = await connection();
    await user.click(c.getByRole("button", { name: "Sostituisci token" }));
    await user.type(c.getByLabelText("Token del bot"), TOKEN);
    await user.click(c.getByRole("button", { name: "Rileva gruppi" }));
    await c.findByRole("list", { name: "Gruppi rilevati" });
    expect(mutationsHoldingToken(queryClient)).not.toHaveLength(0);
    const chat = c.getByLabelText("ID del gruppo");
    await user.clear(chat);
    await user.type(chat, "-4001");
    await user.click(c.getByRole("button", { name: "Salva" }));
    await waitFor(() => expect(configPuts).toEqual([{ bot_token: TOKEN, chat_id: "-4001" }]));
    await waitFor(() => expect(mutationsHoldingToken(queryClient)).toHaveLength(0));
  });

  it("no mutation keeps the token after a failed save and Annulla", async () => {
    server.use(
      http.put("*/api/telegram/config", () =>
        HttpResponse.json({ code: "INVALID_ORDER", message: "Token del bot non valido", details: { field: "bot_token" } }, { status: 400 }),
      ),
    );
    const user = userEvent.setup();
    const { queryClient } = renderApp("/notifiche");
    const c = await connection();
    await user.click(c.getByRole("button", { name: "Sostituisci token" }));
    await user.type(c.getByLabelText("Token del bot"), TOKEN);
    await user.click(c.getByRole("button", { name: "Salva" }));
    expect(await c.findByText("Token del bot non valido")).toBeInTheDocument();
    expect(mutationsHoldingToken(queryClient)).not.toHaveLength(0);
    await user.click(c.getByRole("button", { name: "Annulla" }));
    await waitFor(() => expect(mutationsHoldingToken(queryClient)).toHaveLength(0));
    expect(c.queryByText("Token del bot non valido")).not.toBeInTheDocument();
  });

  it("chat id follows the server: normalised on save, updated when untouched", async () => {
    const user = userEvent.setup();
    const { queryClient } = renderApp("/notifiche");
    const c = await connection();
    const chat = c.getByLabelText("ID del gruppo");
    expect(chat).not.toHaveAttribute("inputmode");
    await user.clear(chat);
    await user.type(chat, "@sala_operativa");
    await user.click(c.getByRole("button", { name: "Salva" }));
    await waitFor(() => expect(chat).toHaveValue("-100777"));
    // e.g. the group became a supergroup: the untouched field shows the new id
    settings = { ...settings, chat_id: "-100888", chat_title: "Sala migrata" };
    await queryClient.invalidateQueries({ queryKey: ["telegram"], exact: true });
    await waitFor(() => expect(chat).toHaveValue("-100888"));
    expect(c.getByText("Sala migrata")).toBeInTheDocument();
  });

  it("sections are labelled regions", async () => {
    renderApp("/notifiche");
    expect(await screen.findByRole("region", { name: "Ultimi messaggi" })).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Decisioni" })).toBeInTheDocument();
  });

  it("server error shown under the field", async () => {
    server.use(
      http.put("*/api/telegram/config", () =>
        HttpResponse.json(
          { code: "INVALID_ORDER", message: "Token del bot non valido", details: { field: "bot_token" } },
          { status: 400 },
        ),
      ),
    );
    const user = userEvent.setup();
    renderApp("/notifiche");
    const c = await connection();
    await user.click(c.getByRole("button", { name: "Sostituisci token" }));
    const field = c.getByLabelText("Token del bot");
    await user.type(field, "bad");
    await user.click(c.getByRole("button", { name: "Salva" }));
    await waitFor(() => expect(field).toHaveAccessibleDescription("Token del bot non valido"));
    expect(field).toHaveAttribute("aria-invalid", "true");
    expect(c.getByLabelText("ID del gruppo")).not.toHaveAttribute("aria-invalid");
  });

  it("sends a test message and shows the outcome", async () => {
    const user = userEvent.setup();
    renderApp("/notifiche");
    const c = await connection();
    await user.click(c.getByRole("button", { name: "Invia messaggio di prova" }));
    expect(await c.findByText("Messaggio di prova inviato")).toBeInTheDocument();
    expect(tests).toBe(1);
  });

  it("pause toggle calls PUT /api/telegram/paused", async () => {
    const user = userEvent.setup();
    renderApp("/notifiche");
    const c = await connection();
    expect(c.getByText(/I messaggi restano in coda e partono alla ripresa/)).toBeInTheDocument();
    const toggle = c.getByRole("switch", { name: "Metti in pausa gli invii" });
    expect(toggle).not.toBeChecked();
    await user.click(toggle);
    await waitFor(() => expect(pausedPuts).toEqual([{ paused: true }]));
    await waitFor(() => expect(toggle).toBeChecked());
  });

  it("enabled toggle saves only enabled", async () => {
    const user = userEvent.setup();
    renderApp("/notifiche");
    const c = await connection();
    const toggle = c.getByRole("switch", { name: "Notifiche attive" });
    expect(toggle).toHaveAccessibleDescription(
      "Disattivando, gli eventi di questo periodo non verranno inviati. Per sospendere temporaneamente usa Pausa: i messaggi restano in coda.",
    );
    await user.click(toggle);
    await waitFor(() => expect(configPuts).toEqual([{ enabled: false }]));
  });

  it("retry button only on failed rows and calls retry", async () => {
    const user = userEvent.setup();
    renderApp("/notifiche");
    const table = await screen.findByRole("table", { name: "Ultimi messaggi" });
    await within(table).findByText("Fallito");
    expect(within(table).getAllByText("Sistema")).toHaveLength(2);
    expect(within(table).getByText("Notifier")).toBeInTheDocument();
    expect(within(table).getByText("In coda")).toBeInTheDocument();
    expect(within(table).getByText("Inviato")).toBeInTheDocument();
    const buttons = within(table).getAllByRole("button", { name: "Riprova" });
    expect(buttons).toHaveLength(1);
    await user.click(buttons[0]);
    await waitFor(() => expect(retries).toEqual(["3"]));

    await user.selectOptions(screen.getByLabelText("Stato"), "failed");
    await waitFor(() => expect(outboxUrls.at(-1)).toContain("status=failed"));
  });

  it("retry all failed calls POST retry-failed, shown only with failed messages", async () => {
    const user = userEvent.setup();
    renderApp("/notifiche");
    const region = within(await screen.findByRole("region", { name: "Ultimi messaggi" }));
    const button = await region.findByRole("button", { name: "Riprova tutti i falliti" });
    await user.click(button);
    await waitFor(() => expect(retryAll).toBe(1));
    await waitFor(() => expect(region.queryByRole("button", { name: "Riprova tutti i falliti" })).not.toBeInTheDocument());
  });

  it("retry all with nothing to retry shows a notice", async () => {
    const user = userEvent.setup();
    server.use(http.post("*/api/telegram/outbox/retry-failed", () => HttpResponse.json({ retried: 0 })));
    renderApp("/notifiche");
    const region = within(await screen.findByRole("region", { name: "Ultimi messaggi" }));
    await user.click(await region.findByRole("button", { name: "Riprova tutti i falliti" }));
    expect(await region.findByText("Nessun messaggio da riprovare")).toBeInTheDocument();
  });

  it("skipped messages have their own label", async () => {
    server.use(
      http.get("*/api/telegram/outbox", () =>
        HttpResponse.json({ items: [outboxItem({ id: 5, status: "skipped", last_error: "Non inviato: in pausa da più di 24 ore", sent_at: null })] }),
      ),
    );
    renderApp("/notifiche");
    const table = await screen.findByRole("table", { name: "Ultimi messaggi" });
    expect(await within(table).findByText("Scartato")).toBeInTheDocument();
    expect(within(table).queryByRole("button", { name: "Riprova" })).not.toBeInTheDocument();
  });

  it("decisions filter by kind", async () => {
    const user = userEvent.setup();
    renderApp("/notifiche");
    const table = await screen.findByRole("table", { name: "Decisioni" });
    expect(await within(table).findByText("Veto su TSLA")).toBeInTheDocument();
    expect(within(table).getByText("Proposta")).toBeInTheDocument();
    await user.selectOptions(screen.getByLabelText("Tipo"), "veto");
    await waitFor(() => expect(within(table).queryByText("Comprare AAPL")).not.toBeInTheDocument());
    expect(decisionUrls.at(-1)).toContain("kind=veto");
    expect(within(table).getByText("Veto su TSLA")).toBeInTheDocument();
  });

  it("loads more decisions with before_id", async () => {
    server.use(
      http.get("*/api/decisions", ({ request }) => {
        decisionUrls.push(request.url);
        const before = new URL(request.url).searchParams.get("before_id");
        if (before) return HttpResponse.json({ items: [decision({ id: 1, summary: "Decisione vecchia" })] });
        return HttpResponse.json({
          items: Array.from({ length: 50 }, (_, i) => decision({ id: 100 - i, summary: `Decisione ${100 - i}` })),
        });
      }),
    );
    const user = userEvent.setup();
    renderApp("/notifiche");
    await user.click(await screen.findByRole("button", { name: "Carica altre" }));
    expect(await screen.findByText("Decisione vecchia")).toBeInTheDocument();
    expect(decisionUrls.at(-1)).toContain("before_id=51");
    expect(screen.queryByRole("button", { name: "Carica altre" })).not.toBeInTheDocument();
  });
});

describe("AgentDetail notify permission", () => {
  it("toggle calls PATCH { can_notify }", async () => {
    const user = userEvent.setup();
    renderApp("/agenti/a1");
    const toggle = await screen.findByRole("switch", { name: "Può inviare su Telegram (notify)" });
    expect(toggle).not.toBeChecked();
    await user.click(toggle);
    await waitFor(() => expect(agentPatches).toEqual([{ can_notify: true }]));
  });

  it("halt toggle calls PATCH { can_halt }", async () => {
    const user = userEvent.setup();
    renderApp("/agenti/a1");
    const toggle = await screen.findByRole("switch", { name: "Può sospendere le aperture (halt)" });
    expect(toggle).not.toBeChecked();
    await user.click(toggle);
    await waitFor(() => expect(agentPatches).toEqual([{ can_halt: true }]));
  });
});
