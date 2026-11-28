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
import { resetViewport, setViewportWidth } from "@/test/viewport";

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
  http.get("*/api/portfolios", () => HttpResponse.json([])), // pages reached by navigation
  http.get("*/api/interest-rates", () => HttpResponse.json([])),
  http.get("*/api/settings/app", () =>
    HttpResponse.json({ stale_quote_s: {}, default_spread_bps: {}, timezone: "Europe/Rome" }),
  ),
  http.get("*/api/dashboard", () => new HttpResponse(null, { status: 503 })),
  http.post("*/api/auth/logout", () => new HttpResponse(null, { status: 204 })),
);
beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  killSwitchOn = false;
  server.resetHandlers();
  setTheme("dark");
  setOwnBuild("");
  localStorage.removeItem(SIDEBAR_KEY);
  resetViewport();
});
afterAll(() => server.close());

describe("Layout", () => {
  it("credits TradingView in the footer (logo hidden inside the charts)", async () => {
    renderApp("/");
    const link = await screen.findByRole("link", { name: "TradingView Lightweight Charts" });
    expect(link).toHaveAttribute("href", "https://www.tradingview.com/lightweight-charts/");
  });

  it("shows the Italian sidebar, the app name and the kill-switch state", async () => {
    const user = userEvent.setup();
    renderApp("/");
    const nav = await screen.findByRole("navigation", { name: "Navigazione principale" });
    // «Agenti» and «Sistema» start closed; their headings open them
    expect(within(nav).getAllByRole("link").map((a) => a.textContent)).toEqual([
      "Dashboard", "Portafogli", "Segnali", "Posizioni", "Diario operazioni", "Strategie", "Obiettivi", "Campagne", "Mercati", "Regole automatiche",
    ]);
    for (const name of ["Agenti", "Sistema"]) {
      const heading = within(nav).getByRole("button", { name });
      expect(heading).toHaveAttribute("aria-expanded", "false");
      // what a real browser shows: the closed list must carry the «hidden» class, not only the attribute
      const list = document.getElementById(heading.getAttribute("aria-controls")!)!;
      expect(list.className.split(" ")).toContain("hidden");
      await user.click(heading);
      expect(heading).toHaveAttribute("aria-expanded", "true");
      expect(list.className.split(" ")).not.toContain("hidden");
    }
    expect(within(nav).getAllByRole("link").map((a) => a.textContent)).toEqual([
      "Dashboard",
      "Portafogli",
      "Segnali",
      "Posizioni",
      "Diario operazioni",
      "Strategie",
      "Obiettivi",
      "Campagne",
      "Mercati",
      "Regole automatiche",
      "Agenti",
      "Istruzioni agenti",
      "Costi agenti",
      "Risultati agenti",
      "Notifiche",
      "Decisioni e lezioni",
      "Log MCP",
      "Fiume dei flussi",
      "Salute del sistema",
      "Broker",
      "Riconciliazione",
      "Connettori",
      "Commissioni",
      "Rischio e margine",
      "Registro attività",
      "Registro denaro reale",
      "Impostazioni",
    ]);
    // three groups: Trading, Agenti, Sistema
    const groups = within(nav).getAllByRole("group");
    expect(groups.map((g) => g.getAttribute("aria-label"))).toEqual(["Trading", "Agenti", "Sistema"]);
    expect(within(groups[2]).getByRole("link", { name: "Broker" })).toBeInTheDocument();
    expect(within(groups[2]).getAllByRole("link")[0]).toHaveTextContent("Salute del sistema"); // first of «Sistema»
    const logo = screen.getByTestId("app-logo"); // brand mark next to the name, decorative
    expect(logo).toHaveAttribute("aria-hidden", "true");
    expect(logo.parentElement).toHaveTextContent("SMTrading");
    expect(await screen.findByText("Trading attivo")).toBeInTheDocument();
  });

  it("opens by itself the closed group of the current page", async () => {
    renderApp("/rischio");
    const nav = await screen.findByRole("navigation", { name: "Navigazione principale" });
    expect(within(nav).getByRole("button", { name: "Sistema" })).toHaveAttribute("aria-expanded", "true");
    expect(within(nav).getByRole("link", { name: "Rischio e margine" })).toBeVisible();
    expect(within(nav).getByRole("button", { name: "Agenti" })).toHaveAttribute("aria-expanded", "false");
  });

  it("shows an active kill-switch", async () => {
    killSwitchOn = true;
    renderApp("/");
    expect(await screen.findByText("Kill-switch attivo")).toBeInTheDocument();
  });

  it("toggles the theme on <html> and persists it", async () => {
    const user = userEvent.setup();
    renderApp("/");
    await user.click(await screen.findByRole("button", { name: "Tema scuro: passa a chiaro" }));
    expect(document.documentElement.classList.contains("dark")).toBe(false);
    expect(localStorage.getItem(THEME_KEY)).toBe("light");
    await user.click(screen.getByRole("button", { name: "Tema chiaro: passa a automatico" }));
    expect(localStorage.getItem(THEME_KEY)).toBe("auto");
    await user.click(screen.getByRole("button", { name: "Tema automatico: passa a scuro" }));
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
    await user.click(within(menu()!).getByRole("button", { name: "Sistema" }));
    await user.click(within(menu()!).getByRole("link", { name: "Rischio e margine" }));
    await vi.waitFor(() => expect(router.state.location.pathname).toBe("/rischio"));
    expect(menu()).not.toBeInTheDocument();
  });

  it("on a phone: compact sticky header, drawer with «Salute del sistema», no sideways page scroll", async () => {
    setViewportWidth(375);
    const user = userEvent.setup();
    const { router } = renderApp("/");
    const open = await screen.findByRole("button", { name: "Apri menu" });
    const header = open.closest("header")!;
    expect(header.className).toContain("sticky");
    // icon-only controls keep their names; the kill-switch state stays readable to screen readers
    expect(within(header).getByRole("button", { name: "Esci" })).toBeInTheDocument();
    expect(await within(header).findByText("Trading attivo")).toHaveClass("max-sm:sr-only");
    // the page scrolls only vertically: wide content scrolls inside its own box
    const main = screen.getByRole("main");
    expect(main.className).toContain("overflow-x-hidden");
    expect(main.className).toContain("min-w-0");
    await user.click(open);
    const menu = screen.getByRole("navigation", { name: "Navigazione principale (menu)" });
    const sistema = within(menu).getByRole("group", { name: "Sistema" });
    await user.click(within(sistema).getByRole("button", { name: "Sistema" }));
    await user.click(within(sistema).getByRole("link", { name: "Salute del sistema" }));
    await vi.waitFor(() => expect(router.state.location.pathname).toBe("/salute-sistema"));
    expect(screen.queryByRole("navigation", { name: "Navigazione principale (menu)" })).not.toBeInTheDocument();
    expect(open).toHaveFocus(); // focus back on the hamburger
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
    expect(await screen.findByText("SMTrading (simulato) – denaro virtuale, prezzi di mercato reali")).toBeInTheDocument();
  });

  it("real money armed: no red strip, a red-dot badge with count, names and link; telegram warning kept", async () => {
    server.use(
      http.get("*/api/live/status", () =>
        HttpResponse.json({
          armed_count: 1,
          dry_run_count: 1,
          portfolios: [
            { id: "p1", name: "Reale", live_state: "armed" },
            { id: "p2", name: "Prova", live_state: "dry_run" },
          ],
          telegram_ok: false,
        }),
      ),
    );
    renderApp("/");
    const badge = await screen.findByRole("link", { name: /Denaro reale attivo su 1 portafoglio/ });
    expect(badge).toHaveAttribute("href", "/broker#denaro-reale");
    expect(badge).toHaveAttribute("title", "Denaro reale attivo su 1 portafoglio: Reale · clic per disattivare");
    expect(within(badge).getByTestId("live-money-dot").className).toContain("bg-loss");
    // the full-width red strip is gone (and the modes strip is not shown in its place)
    expect(screen.queryByRole("status", { name: "Modalità di trading" })).not.toBeInTheDocument();
    expect(document.querySelector(".bg-loss.px-6")).toBeNull();
    expect(screen.getByText("Telegram non funziona: gli ordini reali continuano, le notifiche restano in coda")).toBeInTheDocument();
  });

  it("the badge counts the real portfolios in the plural and no Telegram warning when it works", async () => {
    // one armed link, two portfolios on its account
    server.use(
      http.get("*/api/live/status", () =>
        HttpResponse.json({
          armed_count: 1,
          dry_run_count: 0,
          portfolios: [
            { id: "p1", name: "A", live_state: "armed" },
            { id: "p2", name: "B", live_state: "armed" },
          ],
          telegram_ok: true,
        }),
      ),
    );
    renderApp("/");
    const badge = await screen.findByRole("link", { name: /Denaro reale attivo su 2 portafogli/ });
    expect(badge).toHaveAttribute("title", "Denaro reale attivo su 2 portafogli: A, B · clic per disattivare");
    expect(screen.queryByText(/Telegram non funziona/)).not.toBeInTheDocument();
  });

  it("no red-dot badge without an armed link", async () => {
    renderApp("/");
    expect(await screen.findByText("SMTrading (simulato) – denaro virtuale, prezzi di mercato reali")).toBeInTheDocument();
    expect(screen.queryByTestId("live-money-dot")).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /Denaro reale attivo/ })).not.toBeInTheDocument();
  });

  it("shows no Telegram warning without an armed link", async () => {
    server.use(
      http.get("*/api/live/status", () => HttpResponse.json({ armed_count: 0, dry_run_count: 1, portfolios: [], telegram_ok: false })),
    );
    renderApp("/");
    expect(await screen.findByText("SMTrading (simulato) – denaro virtuale, prezzi di mercato reali")).toBeInTheDocument();
    expect(screen.queryByText(/Telegram non funziona/)).not.toBeInTheDocument();
  });

  it("banner simulated and practice text", async () => {
    server.use(http.get("*/api/dashboard/modes", () => HttpResponse.json({ simulazione: 1, paper: 2, reale: 0 })));
    renderApp("/");
    const banner = await screen.findByRole("status", { name: "Modalità di trading" });
    expect(banner).toHaveTextContent("SMTrading (simulato) e conti di prova dei broker – nessun denaro reale");
    expect(banner.className).toContain("text-warn");
  });

  it("falls back to a neutral notice when the modes cannot be read", async () => {
    server.use(http.get("*/api/dashboard/modes", () => new HttpResponse(null, { status: 503 })));
    renderApp("/");
    expect(await screen.findByText("Modalità dei portafogli non verificabile al momento", {}, { timeout: 4000 })).toBeInTheDocument();
  });

  it("small screens: the drawer groups open and close from their heading", async () => {
    const user = userEvent.setup();
    renderApp("/");
    await user.click(await screen.findByRole("button", { name: "Apri menu" }));
    const menu = screen.getByRole("navigation", { name: "Navigazione principale (menu)" });
    const sistema = within(menu).getByRole("button", { name: "Sistema" });
    expect(sistema).toHaveAttribute("aria-expanded", "false"); // closed by default
    expect(within(menu).queryByRole("link", { name: "Broker" })).not.toBeInTheDocument();
    expect(within(menu).getByRole("link", { name: "Dashboard" })).toBeInTheDocument();
    await user.click(sistema);
    expect(sistema).toHaveAttribute("aria-expanded", "true");
    expect(within(menu).getByRole("link", { name: "Broker" })).toBeVisible();
    await user.click(sistema);
    expect(within(menu).queryByRole("link", { name: "Broker" })).not.toBeInTheDocument();
  });

  it("moves focus into the drawer, keeps Tab inside and returns it to the menu button", async () => {
    const user = userEvent.setup();
    renderApp("/");
    const open = await screen.findByRole("button", { name: "Apri menu" });
    await user.click(open);
    const menu = screen.getByRole("navigation", { name: "Navigazione principale (menu)" });
    const links = within(menu).getAllByRole("link");
    const first = within(menu).getByRole("button", { name: "Trading" }); // the first group's heading
    expect(first).toHaveFocus();
    await user.tab({ shift: true });
    expect(within(menu).getByRole("button", { name: "Sistema" })).toHaveFocus(); // wraps backwards (closed last group)
    await user.tab();
    expect(first).toHaveFocus(); // wraps forwards
    for (let i = 0; i < links.length + 2; i++) await user.tab();
    expect(menu.contains(document.activeElement)).toBe(true);
    await user.keyboard("{Escape}");
    expect(open).toHaveFocus();
  });
});
