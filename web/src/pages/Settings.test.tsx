import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { toast } from "sonner";
import { formatBytes } from "@/lib/format";
import { renderApp } from "@/test/render";
import { commonHandlers } from "@/test/handlers";

vi.mock("@/lib/ws", async (orig) => ({
  ...(await orig<typeof import("@/lib/ws")>()),
  useLiveSocket: () => ({ socket: null, status: "open" }),
}));

let pwPosts: unknown[] = [];
let totpEnabled = false;
let totpCalls: { action: string; body: unknown }[] = [];
let meCalls = 0;
let pwForbidden = false;
let tzPuts: unknown[] = [];
const server = setupServer(
  ...commonHandlers,
  http.get("*/api/auth/me", () => {
    meCalls += 1;
    return HttpResponse.json({ email: "admin@example.com", totp_enabled: totpEnabled });
  }),
  http.post("*/api/auth/totp/setup", () =>
    HttpResponse.json({ secret: "JBSWY3DPEHPK3PXP", otpauth_uri: "otpauth://totp/SMTrading:admin%40example.com?secret=JBSWY3DPEHPK3PXP&issuer=SMTrading" }),
  ),
  http.post("*/api/auth/totp/:action", async ({ request, params }) => {
    const body = (await request.json()) as { code: string };
    totpCalls.push({ action: String(params.action), body });
    if (body.code === "000000") return HttpResponse.json({ code: "FORBIDDEN", message: "codice TOTP non valido", details: null }, { status: 403 });
    totpEnabled = params.action === "enable";
    return HttpResponse.json({ email: "admin@example.com", totp_enabled: totpEnabled });
  }),
  http.post("*/api/auth/sessions/revoke-others", () => HttpResponse.json({ revoked: 2 })),
  http.post("*/api/auth/password", async ({ request }) => {
    pwPosts.push(await request.json());
    if (pwForbidden) return HttpResponse.json({ code: "FORBIDDEN", message: "password attuale errata", details: null }, { status: 403 });
    return HttpResponse.json({ ok: true });
  }),
  http.get("*/api/kill-switch", () => HttpResponse.json({ on: false })),
  http.get("*/api/settings/app", () => HttpResponse.json({ stale_quote_s: {}, default_spread_bps: {}, timezone: "Europe/Rome", mcp_hide_mode: true })),
  http.put("*/api/settings/app", async ({ request }) => {
    const body = await request.json();
    tzPuts.push(body);
    return HttpResponse.json({ stale_quote_s: {}, default_spread_bps: {}, ...(body as object) });
  }),
  http.get("*/api/system/backups", () =>
    HttpResponse.json([{ name: "smtrading-2026-11-12.sql.gz", size_bytes: 3 * 1024 * 1024, modified_at: "2026-11-12T03:00:00Z" }]),
  ),
  http.get("*/api/system/verify-chain", () => HttpResponse.json({ ok: true, checked: 42, first_bad_journal_id: null })),
  http.get("*/api/system/verify-balances", () => HttpResponse.json({ ok: false, bad_accounts: ["acc-1"] })),
);
beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  server.resetHandlers();
  tzPuts = [];
  pwPosts = [];
  pwForbidden = false;
  totpEnabled = false;
  totpCalls = [];
  meCalls = 0;
});
afterAll(() => server.close());

describe("Settings", () => {
  it("formats byte sizes", () => {
    expect(formatBytes(512)).toBe("512 B");
    expect(formatBytes(1536)).toBe("1,5 KB");
    expect(formatBytes(3 * 1024 * 1024)).toBe("3 MB");
  });

  it("lists backups and downloads a fresh one with a POST", async () => {
    const user = userEvent.setup();
    const created = vi.fn(() => "blob:backup");
    const revoked = vi.fn();
    Object.assign(URL, { createObjectURL: created, revokeObjectURL: revoked });
    const clicks: string[] = [];
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) {
      clicks.push(this.download);
    });
    let csrf: string | null = null;
    server.use(
      http.post("*/api/system/backup", ({ request }) => {
        csrf = request.headers.get("X-CSRF-Token");
        return new HttpResponse("PGDMP", {
          headers: { "Content-Type": "application/octet-stream", "Content-Disposition": 'attachment; filename="smtrading-20261113-1200.dump"' },
        });
      }),
    );
    document.cookie = "smt_csrf=tok";
    renderApp("/impostazioni");
    expect(await screen.findByText("smtrading-2026-11-12.sql.gz")).toBeInTheDocument();
    expect(screen.getByText("3 MB")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Scarica backup ora" }));
    await waitFor(() => expect(clicks).toEqual(["smtrading-20261113-1200.dump"]));
    expect(csrf).toBe("tok");
    expect(created).toHaveBeenCalledTimes(1);
    click.mockRestore();
  });

  it("shows the server error when a backup is already running", async () => {
    const user = userEvent.setup();
    server.use(
      http.post("*/api/system/backup", () =>
        HttpResponse.json({ code: "RATE_LIMITED", message: "un backup è già in corso", details: null }, { status: 429 }),
      ),
    );
    renderApp("/impostazioni");
    await user.click(await screen.findByRole("button", { name: "Scarica backup ora" }));
    expect(await screen.findByText("un backup è già in corso")).toBeInTheDocument();
  });

  it("runs both integrity checks and shows the outcome", async () => {
    const user = userEvent.setup();
    renderApp("/impostazioni");
    await user.click(await screen.findByRole("button", { name: "Verifica integrità" }));
    expect(await screen.findByText("Catena delle registrazioni integra (42 verificate)")).toBeInTheDocument();
    expect(screen.getByText("Saldi non coerenti: acc-1")).toBeInTheDocument();
  });

  it("saves the timezone", async () => {
    const user = userEvent.setup();
    renderApp("/impostazioni");
    const card = await screen.findByRole("region", { name: "Fuso orario" });
    await user.selectOptions(await within(card).findByRole("combobox"), "Asia/Tokyo");
    await user.click(within(card).getByRole("button", { name: "Salva" }));
    await waitFor(() => expect(tzPuts).toEqual([{ timezone: "Asia/Tokyo" }]));
  });

  it("toggles the MCP hide-mode setting", async () => {
    const user = userEvent.setup();
    renderApp("/impostazioni");
    const box = await screen.findByLabelText(/Nascondi agli agenti la modalità dei portafogli/);
    expect(box).toBeChecked();
    await user.click(box);
    await waitFor(() => expect(tzPuts).toEqual([{ mcp_hide_mode: false }]));
  });

  it("validates the new password client-side", async () => {
    const user = userEvent.setup();
    renderApp("/impostazioni");
    await user.type(await screen.findByLabelText("Password attuale"), "oldpassword1");
    await user.type(screen.getByLabelText("Nuova password"), "short");
    expect(screen.getByText("Almeno 10 caratteri")).toBeInTheDocument();
    await user.clear(screen.getByLabelText("Nuova password"));
    await user.type(screen.getByLabelText("Nuova password"), "longenough123");
    await user.type(screen.getByLabelText("Conferma nuova password"), "different");
    expect(screen.getByText("Le password non coincidono")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Aggiorna password" }));
    expect(pwPosts).toEqual([]);
    await user.clear(screen.getByLabelText("Nuova password"));
    await user.type(screen.getByLabelText("Nuova password"), "oldpassword1");
    expect(screen.getByText("La nuova password deve essere diversa da quella attuale")).toBeInTheDocument();
  });

  it("changes the password and clears the fields", async () => {
    const user = userEvent.setup();
    renderApp("/impostazioni");
    await user.type(await screen.findByLabelText("Password attuale"), "oldpassword1");
    await user.type(screen.getByLabelText("Nuova password"), "longenough123");
    await user.type(screen.getByLabelText("Conferma nuova password"), "longenough123");
    await user.click(screen.getByRole("button", { name: "Aggiorna password" }));
    await waitFor(() => expect(pwPosts).toEqual([{ current: "oldpassword1", new: "longenough123" }]));
    await waitFor(() => expect(screen.getByLabelText("Password attuale")).toHaveValue(""));
    expect(screen.getByLabelText("Nuova password")).toHaveValue("");
  });

  it("shows a wrong current password inline", async () => {
    pwForbidden = true;
    const user = userEvent.setup();
    renderApp("/impostazioni");
    await user.type(await screen.findByLabelText("Password attuale"), "wrongpassword");
    await user.type(screen.getByLabelText("Nuova password"), "longenough123");
    await user.type(screen.getByLabelText("Conferma nuova password"), "longenough123");
    await user.click(screen.getByRole("button", { name: "Aggiorna password" }));
    expect(await screen.findByText("Password attuale errata")).toBeInTheDocument();
  });

  it("sets up TOTP: QR code and secret, server error inline, then enabled", async () => {
    const user = userEvent.setup();
    renderApp("/impostazioni");
    expect(await screen.findByText("non attiva")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Configura" }));
    expect(await screen.findByAltText("Codice QR per l'app di autenticazione")).toHaveAttribute("src", expect.stringMatching(/^data:image\/png;base64,/));
    expect(screen.getByText("JBSWY3DPEHPK3PXP")).toBeInTheDocument();
    const code = screen.getByLabelText("Codice dall'app");
    await user.type(code, "12");
    await user.click(screen.getByRole("button", { name: "Attiva" }));
    expect(screen.getByText("Inserisci le 6 cifre del codice")).toBeInTheDocument();
    expect(totpCalls).toEqual([]);
    await user.clear(code);
    await user.type(code, "000000");
    await user.click(screen.getByRole("button", { name: "Attiva" }));
    expect(await screen.findByText("codice TOTP non valido")).toBeInTheDocument();
    await user.clear(code);
    await user.type(code, "123456");
    const before = meCalls;
    await user.click(screen.getByRole("button", { name: "Attiva" }));
    expect(await screen.findByText("attiva")).toBeInTheDocument();
    expect(totpCalls.at(-1)).toEqual({ action: "enable", body: { code: "123456" } });
    await waitFor(() => expect(meCalls).toBeGreaterThan(before)); // /me refetched (new session)
    expect(screen.queryByText("JBSWY3DPEHPK3PXP")).not.toBeInTheDocument();
  });

  it("disables TOTP with a code", async () => {
    totpEnabled = true;
    const user = userEvent.setup();
    renderApp("/impostazioni");
    expect(await screen.findByText("attiva")).toBeInTheDocument();
    await user.type(screen.getByLabelText("Codice dall'app"), "654321");
    await user.click(screen.getByRole("button", { name: "Disattiva" }));
    expect(await screen.findByText("non attiva")).toBeInTheDocument();
    expect(totpCalls).toEqual([{ action: "disable", body: { code: "654321" } }]);
    expect(screen.getByRole("button", { name: "Configura" })).toBeInTheDocument();
  });

  it("closes the other sessions", async () => {
    const success = vi.spyOn(toast, "success");
    let posted = 0;
    server.use(
      http.post("*/api/auth/sessions/revoke-others", () => {
        posted += 1;
        return HttpResponse.json({ revoked: 2 });
      }),
    );
    const user = userEvent.setup();
    renderApp("/impostazioni");
    await user.click(await screen.findByRole("button", { name: "Chiudi tutte le altre sessioni" }));
    await waitFor(() => expect(success).toHaveBeenCalledWith("Chiuse 2 altre sessioni"));
    expect(posted).toBe(1);
    success.mockRestore();
  });
});
