import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { renderApp } from "@/test/render";
import { commonHandlers } from "@/test/handlers";
import type { PaperclipConfig, PaperclipStatus } from "@/api/types";

vi.mock("@/lib/ws", async (orig) => ({
  ...(await orig<typeof import("@/lib/ws")>()),
  useLiveSocket: () => ({ socket: null, status: "open" }),
}));

const KEY = "pcp_board_secretsecretabcd";
const AGENT_A = "11111111-1111-1111-1111-111111111111";
const AGENT_B = "22222222-2222-2222-2222-222222222222";

let role: "admin" | "viewer" = "admin";
let config: PaperclipConfig;
let status: PaperclipStatus;
let puts: Record<string, unknown>[] = [];

function baseConfig(): PaperclipConfig {
  return {
    base_url: "https://paperclip.example.com",
    company_id: "acme",
    enabled: true,
    board_key_masked: "••••abcd",
    agent_map: {},
    error: null,
  };
}
function baseStatus(): PaperclipStatus {
  return {
    enabled: true,
    isolation: "ok",
    changes: { removed: [], changed: [], added: [] },
    last_ok_at: "2026-11-15T10:00:00Z",
    last_error: null,
    consecutive_errors: 0,
    baseline_at: "2026-11-10T10:00:00Z",
    rebaseline_reason: null,
    baseline_count: 4,
    key_expires_at: null,
    key_expiring: false,
  };
}

const server = setupServer(
  http.get("*/api/auth/me", () => HttpResponse.json({ email: "a@example.com", role, totp_enabled: false })),
  http.get("*/api/kill-switch", () => HttpResponse.json({ on: false })),
  http.get("*/api/settings/app", () => HttpResponse.json({ stale_quote_s: {}, default_spread_bps: {}, timezone: "Europe/Rome", mcp_hide_mode: false })),
  http.get("*/api/system/backups", () => HttpResponse.json([])),
  http.get("*/api/auth/sessions", () => HttpResponse.json([])),
  http.get("*/api/paperclip/config", () => HttpResponse.json(config)),
  http.get("*/api/paperclip/status", () => HttpResponse.json(status)),
  http.put("*/api/paperclip/config", async ({ request }) => {
    const body = (await request.json()) as Record<string, unknown>;
    puts.push(body);
    config = { ...config, ...(body.agent_map ? { agent_map: body.agent_map as Record<string, string> } : {}), board_key_masked: "••••abcd" };
    return HttpResponse.json(config);
  }),
  http.get("*/api/agents", () =>
    HttpResponse.json([
      { id: AGENT_A, name: "Analista", active: true, created_at: "2026-01-01T00:00:00Z", last_seen_at: null, can_notify: false, can_halt: false, portfolios: [] },
      { id: AGENT_B, name: "CEO", active: true, created_at: "2026-01-01T00:00:00Z", last_seen_at: null, can_notify: false, can_halt: false, portfolios: [] },
    ]),
  ),
  http.post("*/api/paperclip/verify", () =>
    HttpResponse.json({
      ok: true,
      policies_count: 3,
      error: null,
      agents: [
        { id: "pc-1", name: "Analista", smt_agent: { id: AGENT_A, name: "Analista" } },
        { id: "pc-2", name: "Trader notturno", smt_agent: null },
      ],
    }),
  ),
  ...commonHandlers, // after the Paperclip handlers above: the first match wins
);
beforeAll(() => server.listen({ onUnhandledRequest: "bypass" }));
afterEach(() => {
  server.resetHandlers();
  role = "admin";
  puts = [];
});
afterAll(() => server.close());

function section() {
  return screen.findByRole("region", { name: "Paperclip" });
}

describe("PaperclipSection", () => {
  it("key never shown, only masked", async () => {
    config = baseConfig();
    status = baseStatus();
    const user = userEvent.setup();
    const { queryClient, container } = renderApp("/impostazioni");
    const region = await section();
    const key = await within(region).findByLabelText("Chiave board");
    expect(key).toHaveAttribute("type", "password");
    expect(key).toHaveAttribute("autocomplete", "new-password");
    expect(key).toHaveValue("");
    expect(key).toHaveAttribute("placeholder", "••••abcd");
    expect(within(region).getByText(/Ultima lettura:/)).toBeInTheDocument();

    await user.type(key, KEY);
    await user.click(within(region).getByRole("button", { name: "Salva" }));
    await waitFor(() => expect(puts).toHaveLength(1));
    expect(puts[0].board_key).toBe(KEY);
    // after the save the field is emptied and nothing keeps the key
    await waitFor(() => expect(key).toHaveValue(""));
    expect(container.innerHTML).not.toContain(KEY);
    expect(JSON.stringify(queryClient.getQueryCache().getAll().map((q) => q.state.data))).not.toContain(KEY);
    await waitFor(() => expect(queryClient.getMutationCache().getAll()).toHaveLength(0));
  });

  it("saving without a new key keeps the stored one", async () => {
    config = baseConfig();
    status = baseStatus();
    const user = userEvent.setup();
    renderApp("/impostazioni");
    const region = await section();
    const url = await within(region).findByLabelText("Indirizzo");
    await user.clear(url);
    await user.type(url, "https://pc.example.org");
    await user.click(within(region).getByRole("button", { name: "Salva" }));
    await waitFor(() => expect(puts).toHaveLength(1));
    expect(puts[0]).toEqual({ base_url: "https://pc.example.org" });
  });

  it("a refused save shows the field error and keeps nothing of the key", async () => {
    config = baseConfig();
    status = baseStatus();
    server.use(
      http.put("*/api/paperclip/config", () =>
        HttpResponse.json({ code: "INVALID_ORDER", message: "la chiave board deve iniziare con pcp_board_", details: { field: "board_key" } }, { status: 400 }),
      ),
    );
    const user = userEvent.setup();
    const { queryClient } = renderApp("/impostazioni");
    const region = await section();
    const key = await within(region).findByLabelText("Chiave board");
    await user.type(key, "wrong-key-1234");
    await user.click(within(region).getByRole("button", { name: "Salva" }));
    expect(await within(region).findByText("la chiave board deve iniziare con pcp_board_")).toBeInTheDocument();
    expect(key).toHaveValue("");
    expect(queryClient.getMutationCache().getAll()).toHaveLength(0);
  });

  it("verify shows agents and mapping", async () => {
    config = baseConfig();
    status = baseStatus();
    const user = userEvent.setup();
    renderApp("/impostazioni");
    const region = await section();
    await user.click(await within(region).findByRole("button", { name: "Verifica" }));
    expect(await within(region).findByText("Regole di isolamento lette: 3")).toBeInTheDocument();
    const table = within(region).getByRole("table", { name: "Agenti Paperclip" });
    const rows = within(table).getAllByRole("row").slice(1);
    expect(rows).toHaveLength(2);
    const first = within(rows[0]).getByRole("combobox", { name: "Agente SMTrading per Analista" });
    expect(first).toHaveValue(AGENT_A);
    const second = within(rows[1]).getByRole("combobox", { name: "Agente SMTrading per Trader notturno" });
    expect(second).toHaveValue("");
    expect(within(second).getByRole("option", { name: "— automatico (per nome) —" })).toBeInTheDocument();
    await user.selectOptions(second, AGENT_B);
    await user.click(within(region).getByRole("button", { name: "Salva" }));
    await waitFor(() => expect(puts).toHaveLength(1));
    expect(puts[0]).toEqual({ agent_map: { "pc-2": AGENT_B } });
    // the old verify result (computed with the previous matches) is cleared after the save
    await waitFor(() => expect(within(region).queryByRole("table", { name: "Agenti Paperclip" })).toBeNull());
  });

  it("shows the read error and the expiry warning", async () => {
    config = baseConfig();
    status = { ...baseStatus(), last_error: "HTTP 401", consecutive_errors: 2, key_expiring: true, key_expires_at: "2026-11-20T00:00:00Z" };
    renderApp("/impostazioni");
    const region = await section();
    expect(await within(region).findByText(/Errore di lettura: HTTP 401/)).toBeInTheDocument();
    expect(within(region).getByText(/La chiave board scade il/)).toBeInTheDocument();
  });

  it("shows the rebaseline note after a configuration change", async () => {
    config = baseConfig();
    status = { ...baseStatus(), isolation: "unknown", baseline_at: null, baseline_count: 0, rebaseline_reason: "config" };
    renderApp("/impostazioni");
    const region = await section();
    expect(await within(region).findByText("Configurazione cambiata: nuova fotografia delle regole al prossimo controllo")).toBeInTheDocument();
  });

  it("shows the expected isolation rules when ok", async () => {
    config = baseConfig();
    status = baseStatus();
    renderApp("/impostazioni");
    const region = await section();
    expect(await within(region).findByText(/Regole di isolamento: 4 attese \(dal /)).toBeInTheDocument();
  });

  it("isolation unknown: shows the reader error and that the alarm is not active", async () => {
    config = baseConfig();
    status = {
      ...baseStatus(),
      isolation: "unknown",
      baseline_at: null,
      baseline_count: 0,
      consecutive_errors: 0,
      last_error: "nessuna regola di isolamento trovata: controlla l'id azienda e i permessi della chiave",
    };
    renderApp("/impostazioni");
    const region = await section();
    expect(await within(region).findByText(/Errore di lettura: nessuna regola di isolamento trovata/)).toBeInTheDocument();
    expect(within(region).getByText(/Regole di isolamento non verificate: l'allarme non è attivo/)).toBeInTheDocument();
    expect(within(region).getByText(/Ultima lettura:/)).toBeInTheDocument();
  });

  it("no isolation warning while the rules are re-read after a configuration change", async () => {
    config = baseConfig();
    status = { ...baseStatus(), isolation: "unknown", baseline_at: null, baseline_count: 0, rebaseline_reason: "config" };
    renderApp("/impostazioni");
    const region = await section();
    await within(region).findByText(/Configurazione cambiata/);
    expect(within(region).queryByText(/l'allarme non è attivo/)).toBeNull();
  });

  it("shows when the status cannot be read", async () => {
    config = baseConfig();
    server.use(http.get("*/api/paperclip/status", () => HttpResponse.json({ code: "INTERNAL", message: "x", details: null }, { status: 500 })));
    renderApp("/impostazioni");
    const region = await section();
    expect(await within(region).findByText("Impossibile leggere lo stato", {}, { timeout: 4000 })).toBeInTheDocument();
  });

  it("shows the unreadable configuration alert", async () => {
    config = { ...baseConfig(), board_key_masked: null, error: "configurazione illeggibile: reinserisci la chiave board" };
    status = baseStatus();
    renderApp("/impostazioni");
    const region = await section();
    expect(await within(region).findByRole("alert")).toHaveTextContent("configurazione illeggibile: reinserisci la chiave board");
  });

  it("verify failure shows the error", async () => {
    config = baseConfig();
    status = baseStatus();
    server.use(http.post("*/api/paperclip/verify", () => HttpResponse.json({ ok: false, agents: [], policies_count: 0, error: "HTTP 401" })));
    const user = userEvent.setup();
    renderApp("/impostazioni");
    const region = await section();
    await user.click(await within(region).findByRole("button", { name: "Verifica" }));
    expect(await within(region).findByRole("alert")).toHaveTextContent("Verifica non riuscita: HTTP 401");
    expect(within(region).queryByRole("table")).toBeNull();
  });

  it("verify with no Paperclip agents shows the empty row", async () => {
    config = baseConfig();
    status = baseStatus();
    server.use(http.post("*/api/paperclip/verify", () => HttpResponse.json({ ok: true, agents: [], policies_count: 2, error: null })));
    const user = userEvent.setup();
    renderApp("/impostazioni");
    const region = await section();
    await user.click(await within(region).findByRole("button", { name: "Verifica" }));
    const table = await within(region).findByRole("table", { name: "Agenti Paperclip" });
    expect(table).toHaveTextContent("Nessun agente in Paperclip");
  });

  it("viewer sees the configuration without edit controls", async () => {
    role = "viewer";
    config = baseConfig();
    status = baseStatus();
    renderApp("/impostazioni");
    const region = await section();
    expect(await within(region).findByText("https://paperclip.example.com")).toBeInTheDocument();
    expect(within(region).getByText("••••abcd")).toBeInTheDocument();
    expect(within(region).queryByRole("button", { name: "Salva" })).toBeNull();
    expect(within(region).queryByRole("button", { name: "Verifica" })).toBeNull();
    expect(within(region).queryByLabelText("Chiave board")).toBeNull();
  });
});
