import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { renderApp } from "@/test/render";
import { setTheme, THEME_KEY } from "@/theme";

vi.mock("@/lib/ws", async (orig) => ({
  ...(await orig<typeof import("@/lib/ws")>()),
  useLiveSocket: () => ({ socket: null, status: "open" }),
}));

let killSwitchOn = false;
const server = setupServer(
  http.get("*/api/auth/me", () => HttpResponse.json({ email: "admin@example.com", totp_enabled: false })),
  http.get("*/api/kill-switch", () => HttpResponse.json({ on: killSwitchOn })),
  http.post("*/api/auth/logout", () => new HttpResponse(null, { status: 204 })),
);
beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  killSwitchOn = false;
  server.resetHandlers();
  setTheme("dark");
});
afterAll(() => server.close());

describe("Layout", () => {
  it("shows the Italian sidebar, the app name and the kill-switch state", async () => {
    renderApp("/");
    const nav = await screen.findByRole("navigation", { name: "Navigazione principale" });
    expect(within(nav).getAllByRole("link").map((a) => a.textContent)).toEqual([
      "Dashboard",
      "Portafogli",
      "Agenti",
      "Commissioni",
      "Rischio e margine",
      "Connettori",
      "Mercati",
      "Log MCP",
      "Impostazioni",
    ]);
    expect(screen.getByText("Trading", { exact: false })).toBeInTheDocument();
    expect(await screen.findByText("Trading attivo")).toBeInTheDocument();
  });

  it("shows an active kill-switch", async () => {
    killSwitchOn = true;
    renderApp("/");
    expect(await screen.findByText("Kill-switch attivo")).toBeInTheDocument();
  });

  it("toggles the theme on <html> and persists it", async () => {
    const user = userEvent.setup();
    renderApp("/");
    await user.click(await screen.findByRole("button", { name: "Passa al tema chiaro" }));
    expect(document.documentElement.classList.contains("dark")).toBe(false);
    expect(localStorage.getItem(THEME_KEY)).toBe("light");
    await user.click(screen.getByRole("button", { name: "Passa al tema scuro" }));
    expect(document.documentElement.classList.contains("dark")).toBe(true);
    expect(localStorage.getItem(THEME_KEY)).toBe("dark");
  });

  it("logs out and goes to /login", async () => {
    const user = userEvent.setup();
    const { router } = renderApp("/rischio");
    expect(await screen.findByRole("heading", { name: "Rischio e margine" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Esci" }));
    await vi.waitFor(() => expect(router.state.location.pathname).toBe("/login"));
  });
});
