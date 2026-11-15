import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { renderApp } from "@/test/render";
import { commonHandlers } from "@/test/handlers";
import type { PaperclipStatus } from "@/api/types";

vi.mock("@/lib/ws", async (orig) => ({
  ...(await orig<typeof import("@/lib/ws")>()),
  useLiveSocket: () => ({ socket: null, status: "open" }),
}));

let role: "admin" | "viewer" = "admin";
let status: PaperclipStatus;
let baselinePosts = 0;

function baseStatus(): PaperclipStatus {
  return {
    enabled: true,
    isolation: "ok",
    changes: { removed: [], changed: [], added: [] },
    last_ok_at: "2026-11-15T10:00:00Z",
    last_error: null,
    consecutive_errors: 0,
    baseline_at: "2026-11-10T10:00:00Z",
    baseline_count: 4,
    key_expires_at: null,
    key_expiring: false,
  };
}

const server = setupServer(
  http.get("*/api/auth/me", () => HttpResponse.json({ email: "a@example.com", role, totp_enabled: false })),
  http.get("*/api/kill-switch", () => HttpResponse.json({ on: false })),
  http.get("*/api/portfolios", () => HttpResponse.json([])),
  http.get("*/api/paperclip/status", () => HttpResponse.json(status)),
  http.post("*/api/paperclip/baseline", () => {
    baselinePosts += 1;
    status = { ...status, isolation: "ok", changes: { removed: [], changed: [], added: [] } };
    return HttpResponse.json(status);
  }),
  ...commonHandlers, // after the Paperclip handlers above: the first match wins
);
beforeAll(() => server.listen({ onUnhandledRequest: "bypass" }));
afterEach(() => {
  server.resetHandlers();
  role = "admin";
  baselinePosts = 0;
});
afterAll(() => server.close());

describe("PaperclipBanner", () => {
  it("banner red on changed with reconfirm", async () => {
    status = { ...baseStatus(), isolation: "changed", changes: { removed: ["p1", "p2"], changed: ["p3"], added: [] } };
    const user = userEvent.setup();
    renderApp("/portafogli");
    const bar = await screen.findByRole("alert", { name: "Isolamento Paperclip" });
    expect(bar).toHaveTextContent("Le regole di isolamento di Paperclip sono cambiate: 2 rimosse, 1 modificate");
    expect(bar.className).toContain("bg-loss");
    await user.click(screen.getByRole("button", { name: "Riconferma" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Riconferma" }));
    await waitFor(() => expect(baselinePosts).toBe(1));
    await waitFor(() => expect(screen.queryByRole("alert", { name: "Isolamento Paperclip" })).toBeNull());
  });

  it("viewer sees the red banner without the reconfirm button", async () => {
    role = "viewer";
    status = { ...baseStatus(), isolation: "changed", changes: { removed: ["p1"], changed: [], added: [] } };
    renderApp("/portafogli");
    expect(await screen.findByRole("alert", { name: "Isolamento Paperclip" })).toHaveTextContent("1 rimosse, 0 modificate");
    expect(screen.queryByRole("button", { name: "Riconferma" })).toBeNull();
  });

  it("banner yellow after 3 errors", async () => {
    status = { ...baseStatus(), consecutive_errors: 3, last_error: "non raggiungibile" };
    renderApp("/portafogli");
    const bar = await screen.findByRole("alert", { name: "Paperclip non raggiungibile" });
    expect(bar).toHaveTextContent("Paperclip non raggiungibile");
    expect(bar.className).toContain("bg-warn");
  });

  it.each([
    ["2 errors", { consecutive_errors: 2 }],
    ["the reader off", { enabled: false, isolation: "off" as const, consecutive_errors: 5 }],
  ])("no banner with %s", async (_, patch) => {
    status = { ...baseStatus(), ...patch };
    const { queryClient } = renderApp("/portafogli");
    await screen.findByRole("navigation", { name: "Navigazione principale" });
    await waitFor(() => expect(queryClient.getQueryState(["paperclip", "status"])?.status).toBe("success"));
    expect(screen.queryByRole("alert", { name: /Paperclip/ })).toBeNull();
  });

  it("shows the key expiry warning", async () => {
    status = { ...baseStatus(), key_expiring: true, key_expires_at: "2026-11-20T09:00:00Z" };
    renderApp("/portafogli");
    expect(await screen.findByRole("alert", { name: "Scadenza chiave Paperclip" })).toHaveTextContent("La chiave board di Paperclip scade il");
  });
});
