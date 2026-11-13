import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { formatBytes } from "@/lib/format";
import { renderApp } from "@/test/render";

vi.mock("@/lib/ws", async (orig) => ({
  ...(await orig<typeof import("@/lib/ws")>()),
  useLiveSocket: () => ({ socket: null, status: "open" }),
}));

let tzPuts: unknown[] = [];
const server = setupServer(
  http.get("*/api/auth/me", () => HttpResponse.json({ email: "admin@example.com", totp_enabled: false })),
  http.get("*/api/kill-switch", () => HttpResponse.json({ on: false })),
  http.get("*/api/settings/app", () => HttpResponse.json({ stale_quote_s: {}, default_spread_bps: {}, timezone: "Europe/Rome" })),
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
});
afterAll(() => server.close());

describe("Settings", () => {
  it("formats byte sizes", () => {
    expect(formatBytes(512)).toBe("512 B");
    expect(formatBytes(1536)).toBe("1,5 KB");
    expect(formatBytes(3 * 1024 * 1024)).toBe("3 MB");
  });

  it("lists backups and links the manual download", async () => {
    renderApp("/impostazioni");
    expect(await screen.findByText("smtrading-2026-11-12.sql.gz")).toBeInTheDocument();
    expect(screen.getByText("3 MB")).toBeInTheDocument();
    const link = screen.getByRole("link", { name: "Scarica backup ora" });
    expect(link).toHaveAttribute("href", "/api/system/backup");
    expect(link).toHaveAttribute("download");
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
    const select = await screen.findByLabelText("Fuso orario");
    await user.selectOptions(select, "Asia/Tokyo");
    await user.click(screen.getByRole("button", { name: "Salva" }));
    await waitFor(() => expect(tzPuts).toEqual([{ timezone: "Asia/Tokyo" }]));
  });
});
