import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import type { AdminUser, LiveLink, PortalUser } from "@/api/types";
import { renderApp } from "@/test/render";
import { commonHandlers } from "@/test/handlers";

vi.mock("@/lib/ws", async (orig) => ({
  ...(await orig<typeof import("@/lib/ws")>()),
  useLiveSocket: () => ({ socket: null, status: "open" }),
}));

const TEMP = "Tq7xKpW3mZr9bYcN4hVd";

const portalUser = (over: Partial<PortalUser> = {}): PortalUser => ({
  id: "u2",
  email: "anna@example.com",
  role: "viewer",
  totp_enabled: false,
  must_change_password: false,
  active: true,
  disabled_at: null,
  last_login_at: "2026-11-14T09:00:00Z",
  created_at: "2026-11-01T09:00:00Z",
  created_by: "admin@example.com",
  ...over,
});

const liveLink: LiveLink = {
  id: "l1",
  name: "Alpaca vero",
  kind: "alpaca_live",
  live_state: "armed",
  armed_at: "2026-11-14T10:00:00Z",
  armed_by: "admin@example.com",
  disarmed_at: null,
  disarmed_reason: null,
  key_masked: "••••0001",
  account_masked: "••••1234",
  snapshot: null,
  last_test_at: null,
  health_failures: 0,
  portfolio: null,
};

let me: AdminUser = { email: "admin@example.com", totp_enabled: false, role: "admin", must_change_password: false };
let users: PortalUser[] = [];
let killOn = false;
let posts: { path: string; body: unknown }[] = [];
let armed = 0;

async function body(request: Request): Promise<unknown> {
  const text = await request.text();
  return text ? JSON.parse(text) : null;
}

// before the common handlers: the first matching handler answers
const server = setupServer(
  http.get("*/api/auth/me", () => HttpResponse.json(me)),
  http.get("*/api/live/status", () =>
    HttpResponse.json({
      armed_count: armed,
      dry_run_count: 0,
      portfolios: armed ? [{ id: "p1", name: "Reale", live_state: "armed" }] : [],
      telegram_ok: true,
    }),
  ),
  http.post("*/api/auth/password", async ({ request }) => {
    posts.push({ path: "password", body: await body(request) });
    me = { ...me, must_change_password: false };
    return HttpResponse.json({ ok: true });
  }),
  http.get("*/api/users", () => HttpResponse.json(users)),
  http.post("*/api/users", async ({ request }) => {
    const b = (await body(request)) as { email: string; role: "admin" | "viewer"; totp?: string };
    posts.push({ path: "create", body: b });
    if (b.role === "admin" && me.totp_enabled && !b.totp) {
      return HttpResponse.json({ code: "FORBIDDEN", message: "codice TOTP richiesto", details: { field: "totp", totp_required: true } }, { status: 403 });
    }
    const u = portalUser({ id: "u9", email: b.email, role: b.role, must_change_password: true, last_login_at: null });
    users = [...users, u];
    return HttpResponse.json({ user: u, temp_password: TEMP });
  }),
  http.post("*/api/users/:id/reset-password", async ({ request, params }) => {
    posts.push({ path: `reset-password/${params.id}`, body: await body(request) });
    return HttpResponse.json({ user: portalUser({ must_change_password: true }), temp_password: TEMP });
  }),
  http.put("*/api/users/:id/role", async ({ request, params }) => {
    const b = (await body(request)) as { role: "admin" | "viewer" };
    posts.push({ path: `role/${params.id}`, body: b });
    users = users.map((u) => (u.id === params.id ? { ...u, role: b.role } : u));
    return HttpResponse.json({ user: users.find((u) => u.id === params.id) });
  }),
  http.post("*/api/users/:id/:action", async ({ request, params }) => {
    posts.push({ path: `${params.action}/${params.id}`, body: await body(request) });
    users = users.map((u) => (u.id === params.id ? { ...u, active: params.action === "enable" } : u));
    return HttpResponse.json({ user: users.find((u) => u.id === params.id) });
  }),
  http.get("*/api/kill-switch", () => HttpResponse.json({ on: killOn })),
  http.post("*/api/kill-switch", async ({ request }) => {
    const b = (await body(request)) as { on: boolean };
    posts.push({ path: "kill-switch", body: b });
    killOn = b.on;
    return HttpResponse.json({ on: b.on, canceled: 0 });
  }),
  http.get("*/api/settings/app", () => HttpResponse.json({ stale_quote_s: {}, default_spread_bps: {}, timezone: "Europe/Rome", mcp_hide_mode: false })),
  http.get("*/api/system/backups", () => HttpResponse.json([])),
  http.get("*/api/risk-profiles", () => HttpResponse.json([])),
  http.get("*/api/interest-rates", () => HttpResponse.json([])),
  http.get("*/api/portfolios", () => HttpResponse.json([])),
  http.get("*/api/broker-links", () => HttpResponse.json([])),
  http.get("*/api/live-links", () => HttpResponse.json([liveLink])),
  http.post("*/api/live-links/:id/disarm", ({ params }) => {
    posts.push({ path: `disarm/${params.id}`, body: null });
    return HttpResponse.json({ ...liveLink, live_state: "configured", orders: [], settled: true });
  }),
  ...commonHandlers,
);
beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  server.resetHandlers();
  me = { email: "admin@example.com", totp_enabled: false, role: "admin", must_change_password: false };
  users = [];
  killOn = false;
  posts = [];
  armed = 0;
});
afterAll(() => server.close());

const viewer = () => {
  me = { email: "vera@example.com", totp_enabled: false, role: "viewer", must_change_password: false };
};

describe("Impostazioni → Utenti", () => {
  it("lists the users with role, TOTP, last login and state", async () => {
    users = [
      portalUser({ id: "u1", email: "admin@example.com", role: "admin", totp_enabled: true }),
      portalUser(),
      portalUser({ id: "u3", email: "off@example.com", active: false }),
    ];
    renderApp("/impostazioni");
    const table = await screen.findByRole("table", { name: "Utenti" });
    await within(table).findByText("anna@example.com");
    const anna = within(table).getByText("anna@example.com").closest("tr")!;
    expect(within(anna).getByText("Sola lettura")).toBeInTheDocument();
    expect(within(anna).getByText("No")).toBeInTheDocument();
    expect(within(anna).getByText("Attivo")).toBeInTheDocument();
    const self = within(table).getByText("admin@example.com").closest("tr")!;
    expect(within(self).getByText("Sei tu")).toBeInTheDocument(); // no actions on oneself
    expect(within(self).queryByRole("button")).not.toBeInTheDocument();
    const off = within(table).getByText("off@example.com").closest("tr")!;
    expect(within(off).getByText("Disattivato")).toBeInTheDocument();
    expect(within(off).getByRole("button", { name: "Riattiva" })).toBeInTheDocument();
  });

  it("creates a user and shows the temporary password once, with a copy button", async () => {
    const user = userEvent.setup();
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    renderApp("/impostazioni");
    const form = await screen.findByRole("form", { name: "Nuovo utente" });
    await user.type(within(form).getByLabelText("Email"), "nuovo@example.com");
    await user.click(within(form).getByRole("button", { name: "Crea utente" }));
    const dialog = await screen.findByRole("dialog", { name: "Password temporanea" });
    expect(within(dialog).getByLabelText("Password temporanea")).toHaveTextContent(TEMP);
    expect(within(dialog).getByText(/Non verrà più mostrata/)).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Copia" }));
    expect(writeText).toHaveBeenCalledWith(TEMP);
    expect(posts[0]).toEqual({ path: "create", body: { email: "nuovo@example.com", role: "viewer" } });
    await user.click(within(dialog).getByRole("button", { name: "Ho copiato la password" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(screen.queryByText(TEMP)).not.toBeInTheDocument(); // gone for good
    expect(await screen.findByText("nuovo@example.com")).toBeInTheDocument();
  });

  it("asks for the actor's TOTP to create an admin", async () => {
    const user = userEvent.setup();
    me = { ...me, totp_enabled: true };
    renderApp("/impostazioni");
    const form = await screen.findByRole("form", { name: "Nuovo utente" });
    await user.type(within(form).getByLabelText("Email"), "capo@example.com");
    await user.selectOptions(within(form).getByLabelText("Ruolo"), "admin");
    await user.click(within(form).getByRole("button", { name: "Crea utente" }));
    const dialog = await screen.findByRole("dialog", { name: "Creare capo@example.com?" });
    await user.type(within(dialog).getByLabelText("Codice TOTP"), "123456");
    await user.click(within(dialog).getByRole("button", { name: "Crea" }));
    expect(await screen.findByRole("dialog", { name: "Password temporanea" })).toBeInTheDocument();
    expect(posts).toEqual([{ path: "create", body: { email: "capo@example.com", role: "admin", totp: "123456" } }]);
  });

  it("changes a role, disables a user and resets a password after confirmation", async () => {
    const user = userEvent.setup();
    users = [portalUser()];
    renderApp("/impostazioni");
    const table = await screen.findByRole("table", { name: "Utenti" });
    await user.click(await within(table).findByRole("button", { name: "Rendi amministratore" }));
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Cambia ruolo" }));
    await waitFor(() => expect(posts).toContainEqual({ path: "role/u2", body: { role: "admin" } }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());

    await user.click(within(table).getByRole("button", { name: "Disattiva" }));
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Disattiva" }));
    await waitFor(() => expect(posts).toContainEqual({ path: "disable/u2", body: null }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());

    await user.click(within(table).getByRole("button", { name: "Reimposta password" }));
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Reimposta" }));
    const shown = await screen.findByRole("dialog", { name: "Password temporanea" });
    expect(within(shown).getByLabelText("Password temporanea")).toHaveTextContent(TEMP);
  });

  it("is not shown to a read-only user", async () => {
    viewer();
    renderApp("/impostazioni");
    expect(await screen.findByText("Cambia password")).toBeInTheDocument();
    expect(screen.queryByRole("table", { name: "Utenti" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Scarica backup ora" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Chiudi tutte le altre sessioni" })).not.toBeInTheDocument();
    expect(screen.getByText("Autenticazione a due fattori")).toBeInTheDocument(); // own TOTP stays
  });
});

describe("forced password change", () => {
  it("shows only the «Cambia password» form until the temporary password is changed", async () => {
    const user = userEvent.setup();
    me = { email: "nuovo@example.com", totp_enabled: false, role: "viewer", must_change_password: true };
    renderApp("/impostazioni");
    expect(await screen.findByText("Cambia password")).toBeInTheDocument();
    expect(screen.getByText(/password temporanea: scegline una nuova/)).toBeInTheDocument();
    expect(screen.queryByRole("navigation")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Esci" })).toBeInTheDocument();
    await user.type(screen.getByLabelText("Password temporanea"), TEMP);
    await user.type(screen.getByLabelText("Nuova password"), "la-mia-password-1");
    await user.type(screen.getByLabelText("Conferma nuova password"), "la-mia-password-1");
    await user.click(screen.getByRole("button", { name: "Aggiorna password" }));
    await waitFor(() => expect(posts).toEqual([{ path: "password", body: { current: TEMP, new: "la-mia-password-1" } }]));
    expect((await screen.findAllByRole("navigation")).length).toBeGreaterThan(0);
    expect(await screen.findByRole("heading", { name: "Impostazioni" })).toBeInTheDocument();
    expect(screen.queryByText(/password temporanea: scegline una nuova/)).not.toBeInTheDocument();
  });
});

describe("read-only mode", () => {
  it("marks the header and hides the create buttons", async () => {
    viewer();
    renderApp("/portafogli");
    expect(await screen.findByText("Sola lettura")).toBeInTheDocument();
    await screen.findByRole("heading", { name: "Portafogli" });
    expect(screen.queryByRole("button", { name: /Nuovo portafoglio/ })).not.toBeInTheDocument();
  });

  it("an admin sees the create buttons", async () => {
    renderApp("/portafogli");
    expect(await screen.findByRole("button", { name: /Nuovo portafoglio/ })).toBeInTheDocument();
    expect(screen.queryByText("Sola lettura")).not.toBeInTheDocument();
  });

  it("lets a viewer turn the kill-switch on, not off", async () => {
    const user = userEvent.setup();
    viewer();
    renderApp("/rischio");
    await user.click(await screen.findByRole("button", { name: "Attiva kill-switch" }));
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Attiva" }));
    await waitFor(() => expect(posts).toEqual([{ path: "kill-switch", body: { on: true, cancel_open: false } }]));
    const off = await screen.findByRole("button", { name: "Disattiva kill-switch" });
    expect(off).toBeDisabled();
    expect(screen.queryByRole("button", { name: /Nuovo profilo/ })).not.toBeInTheDocument();
  });

  it("keeps the red banner «Disattiva» link and the disarm button for a viewer", async () => {
    const user = userEvent.setup();
    viewer();
    armed = 1;
    renderApp("/broker");
    const banner = await screen.findByRole("status", { name: "Modalità di trading" });
    expect(within(banner).getByRole("link", { name: "Disattiva" })).toHaveAttribute("href", "/broker#denaro-reale");
    const card = await screen.findByLabelText("Denaro reale Alpaca vero");
    await user.click(within(card).getByRole("button", { name: "Disattiva denaro reale" }));
    await waitFor(() => expect(posts).toEqual([{ path: "disarm/l1", body: null }]));
    // everything else on the real-money link is for admins
    expect(within(card).queryByRole("button", { name: "Sostituisci chiavi" })).not.toBeInTheDocument();
    expect(within(card).queryByRole("button", { name: /Attiva denaro reale/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Nuovo collegamento reale/ })).not.toBeInTheDocument();
  });
});
