import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { renderApp } from "@/test/render";
import { setTheme, THEME_KEY } from "@/theme";
import { setOwnBuild } from "@/lib/buildVersion";
import { SIDEBAR_KEY } from "@/lib/sidebarState";
import { commonHandlers } from "@/test/handlers";

vi.mock("@/lib/ws", async (orig) => ({
  ...(await orig<typeof import("@/lib/ws")>()),
  useLiveSocket: () => ({ socket: null, status: "open" }),
}));

let killSwitchOn = false;
const server = setupServer(
  ...commonHandlers,
  http.get("*/api/auth/me", () => HttpResponse.json({ email: "admin@example.com", totp_enabled: false })),
  http.get("*/api/kill-switch", () => HttpResponse.json({ on: killSwitchOn })),
  http.get("*/api/risk-profiles", () => HttpResponse.json([])),
  http.get("*/api/interest-rates", () => HttpResponse.json([])),
  http.get("*/api/settings/app", () =>
    HttpResponse.json({ stale_quote_s: {}, default_spread_bps: {}, timezone: "Europe/Rome" }),
  ),
  http.get("*/api/dashboard", () => new HttpResponse(null, { status: 503 })),
  http.get("*/api/dashboard/equity", () => new HttpResponse(null, { status: 503 })),
  http.post("*/api/auth/logout", () => new HttpResponse(null, { status: 204 })),
);
beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  killSwitchOn = false;
  server.resetHandlers();
  setTheme("dark");
  setOwnBuild("");
  localStorage.removeItem(SIDEBAR_KEY);
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
      "Broker",
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

  it("collapses the sidebar to an icon rail and remembers it", async () => {
    const user = userEvent.setup();
    const first = renderApp("/");
    const collapse = await screen.findByRole("button", { name: "Comprimi menu" });
    await user.click(collapse);
    expect(localStorage.getItem(SIDEBAR_KEY)).toBe("1");
    const nav = screen.getByRole("navigation", { name: "Navigazione principale" });
    const dash = within(nav).getByRole("link", { name: "Dashboard" });
    expect(dash).toHaveAttribute("title", "Dashboard"); // label as tooltip
    first.unmount();

    renderApp("/");
    const expand = await screen.findByRole("button", { name: "Espandi menu" });
    expand.focus();
    await user.keyboard("{Enter}"); // keyboard-operable
    expect(screen.getByRole("button", { name: "Comprimi menu" })).toBeInTheDocument();
    expect(localStorage.getItem(SIDEBAR_KEY)).toBe("0");
  });

  it("starts expanded when the storage cannot be read", async () => {
    const spy = vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    try {
      renderApp("/");
      expect(await screen.findByRole("button", { name: "Comprimi menu" })).toBeInTheDocument();
    } finally {
      spy.mockRestore();
    }
  });

  it("opens the mobile drawer and closes it on Esc, backdrop and navigation", async () => {
    const user = userEvent.setup();
    const { router } = renderApp("/");
    const open = await screen.findByRole("button", { name: "Apri menu" });
    await user.click(open);
    const menu = () => screen.queryByRole("navigation", { name: "Navigazione principale (menu)" });
    expect(menu()).toBeInTheDocument();
    await user.keyboard("{Escape}");
    expect(menu()).not.toBeInTheDocument();

    await user.click(open);
    await user.click(screen.getByRole("button", { name: "Chiudi menu" }));
    expect(menu()).not.toBeInTheDocument();

    await user.click(open);
    await user.click(within(menu()!).getByRole("link", { name: "Rischio e margine" }));
    await vi.waitFor(() => expect(router.state.location.pathname).toBe("/rischio"));
    expect(menu()).not.toBeInTheDocument();
  });

  it("offers a reload when the server runs another build", async () => {
    setOwnBuild("old-build");
    server.use(
      http.get("*/api/kill-switch", () => HttpResponse.json({ on: false }, { headers: { "X-SMT-Build": "new-build" } })),
    );
    renderApp("/");
    expect(await screen.findByText("Nuova versione disponibile — Ricarica")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Ricarica" })).toBeInTheDocument();
  });

  it("shows no reload banner when the builds match", async () => {
    setOwnBuild("same");
    server.use(
      http.get("*/api/kill-switch", () => HttpResponse.json({ on: false }, { headers: { "X-SMT-Build": "same" } })),
    );
    renderApp("/");
    expect(await screen.findByText("Trading attivo")).toBeInTheDocument();
    expect(screen.queryByText("Nuova versione disponibile — Ricarica")).not.toBeInTheDocument();
  });

  it("shows the mode banner from the modes endpoint, and a neutral notice if it fails", async () => {
    renderApp("/");
    expect(await screen.findByText("Simulazione – denaro virtuale, prezzi di mercato reali")).toBeInTheDocument();
  });

  it("falls back to a neutral notice when the modes cannot be read", async () => {
    server.use(http.get("*/api/dashboard/modes", () => new HttpResponse(null, { status: 503 })));
    renderApp("/");
    expect(await screen.findByText("Modalità dei portafogli non verificabile al momento", {}, { timeout: 4000 })).toBeInTheDocument();
  });
});
