import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import type { Tv, TvArea, TvSendResult } from "@/api/tvTypes";
import { commonHandlers } from "@/test/handlers";
import { renderApp } from "@/test/render";

vi.mock("@/lib/ws", async (orig) => ({
  ...(await orig<typeof import("@/lib/ws")>()),
  useLiveSocket: () => ({ socket: null, status: "open" }),
}));

const LINK = "https://192.168.30.7/tv#token=tv-display.abc.sig";

const tv = (over: Partial<Tv> = {}): Tv => ({
  id: "t1",
  name: "Salotto",
  host: "192.168.30.84",
  model: "UE85DU7190UXZT",
  paired: false,
  paired_at: null,
  pairing: { state: "idle", message: null, started_at: null },
  last_used_at: null,
  last_ok: null,
  last_message: null,
  revoked_at: null,
  created_at: "2026-11-28T09:00:00Z",
  ...over,
});

const baseArea = (over: Partial<TvArea> = {}): TvArea => ({
  screens: [
    { key: "flow", label: "Fiume dei flussi" },
    { key: "campaign", label: "Campagna e portafogli" },
    { key: "health", label: "Salute e avvisi" },
  ],
  tvs: [],
  display: { screen: "campaign", rotation: false, interval_s: 30, version: 0, updated_at: "2026-11-28T09:00:00Z", portal_url: null },
  sound: { enabled: false, volume: 40, quiet_start: "22:00", quiet_end: "08:00", tz: "Europe/Rome", quiet_now: false },
  link: { active: false, created_at: null, url: null },
  portal_url_detected: "https://192.168.30.7",
  can_discover: true,
  suggested_subnet: "192.168.30.0/24",
  interval: { min: 15, max: 300 },
  network_note: "I comandi alla TV partono dal server di SMTrading: la TV deve essere raggiungibile da lì.",
  ...over,
});

let area: TvArea = baseArea();
let role = "admin";
let sendResult: TvSendResult = { ok: true, message: "Schermata aperta sulla TV", url: LINK, powered_on: true, app_id: "org.tizen.browser", confirmed: true };
const calls: { method: string; path: string; body: unknown }[] = [];
const record = async (request: Request) => {
  const text = await request.text();
  calls.push({ method: request.method, path: new URL(request.url).pathname, body: text ? JSON.parse(text) : null });
};

const server = setupServer(
  ...commonHandlers,
  http.get("*/api/auth/me", () => HttpResponse.json({ email: "matteo@example.com", role, totp_enabled: false })),
  http.get("*/api/kill-switch", () => HttpResponse.json({ on: false })),
  http.get("*/api/tv", () => HttpResponse.json(area)),
  http.get("*/api/tv/screens/:id/power", () => HttpResponse.json({ power: "standby" })),
  http.post("*/api/tv/screens", async ({ request }) => {
    await record(request);
    area = { ...area, tvs: [...area.tvs, tv({ id: "t-new", host: (calls.at(-1)!.body as { host: string }).host, name: "TV nuova" })] };
    return HttpResponse.json(area);
  }),
  http.delete("*/api/tv/screens/:id", async ({ request }) => {
    await record(request);
    area = { ...area, tvs: [] };
    return HttpResponse.json(area);
  }),
  http.post("*/api/tv/screens/:id/pair", async ({ request }) => {
    await record(request);
    area = { ...area, tvs: [tv({ pairing: { state: "waiting", message: null, started_at: new Date().toISOString() } })] };
    return HttpResponse.json(area);
  }),
  http.post("*/api/tv/screens/:id/send", async ({ request }) => {
    await record(request);
    return HttpResponse.json(sendResult);
  }),
  http.post("*/api/tv/screens/:id/power-off", async ({ request }) => {
    await record(request);
    return HttpResponse.json({ ok: true, message: "Schermo spento" });
  }),
  http.put("*/api/tv/display", async ({ request }) => {
    await record(request);
    area = { ...area, display: { ...area.display, version: area.display.version + 1 } };
    return HttpResponse.json(area);
  }),
  http.post("*/api/tv/link/rotate", async ({ request }) => {
    await record(request);
    area = { ...area, link: { active: true, created_at: "2026-11-28T10:00:00Z", url: LINK } };
    return HttpResponse.json(area);
  }),
  http.post("*/api/tv/link/revoke", async ({ request }) => {
    await record(request);
    area = { ...area, link: { active: false, created_at: null, url: null } };
    return HttpResponse.json(area);
  }),
  http.post("*/api/tv/discover", async ({ request }) => {
    await record(request);
    return HttpResponse.json({
      subnet: "192.168.30.0/24",
      devices: [{ host: "192.168.30.84", name: "85\" Crystal UHD", model: "UE85DU7190UXZT", resolution: "3840x2160", mac: "aa", power: "standby" }],
    });
  }),
  http.get("*/api/tv/admin/screens", () =>
    HttpResponse.json({
      items: [
        {
          id: "t9", user_id: "u2", user_email: "luca@example.com", name: "Camera", host: "192.168.30.90", model: null, paired: true, paired_at: null,
          last_used_at: "2026-11-27T20:00:00Z", last_ok: true, revoked_at: null, revoked_by: null, created_at: "2026-11-20T10:00:00Z",
        },
      ],
    }),
  ),
  http.post("*/api/tv/admin/screens/:id/revoke", async ({ request }) => {
    await record(request);
    return HttpResponse.json({});
  }),
);
beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
beforeEach(() => {
  area = baseArea();
  role = "admin";
  sendResult = { ok: true, message: "Schermata aperta sulla TV", url: LINK, powered_on: true, app_id: "org.tizen.browser", confirmed: true };
  calls.length = 0;
});
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

const last = (path: string) => calls.filter((c) => c.path === path).at(-1);

describe("Schermo TV (portal)", () => {
  it("not configured: no TV yet, the network note, add one by address", async () => {
    const user = userEvent.setup();
    renderApp("/schermo-tv");
    expect(await screen.findByRole("heading", { name: "Schermo TV" })).toBeInTheDocument();
    expect(await screen.findByText(/Nessuna TV: aggiungila qui sotto/)).toBeInTheDocument();
    expect(screen.getByText(/I comandi alla TV partono dal server di SMTrading/)).toBeInTheDocument();
    await user.type(screen.getByLabelText("Indirizzo della TV"), "192.168.30.84");
    await user.click(screen.getByRole("button", { name: "Aggiungi" }));
    expect(await screen.findByRole("listitem", { name: "TV nuova" })).toBeInTheDocument();
    expect(last("/api/tv/screens")?.body).toEqual({ host: "192.168.30.84" });
  });

  it("an address refused by the server: its message", async () => {
    const user = userEvent.setup();
    server.use(
      http.post("*/api/tv/screens", () =>
        HttpResponse.json({ code: "INVALID_ORDER", message: "a quell'indirizzo non risponde una TV Samsung", details: { field: "host" } }, { status: 400 }),
      ),
    );
    renderApp("/schermo-tv");
    await user.type(await screen.findByLabelText("Indirizzo della TV"), "192.168.30.5");
    await user.click(screen.getByRole("button", { name: "Aggiungi" }));
    expect(await screen.findByText("a quell'indirizzo non risponde una TV Samsung")).toBeInTheDocument();
  });

  it("admins: «Cerca TV in rete» lists what it finds and adds it", async () => {
    const user = userEvent.setup();
    renderApp("/schermo-tv");
    await user.click(await screen.findByRole("button", { name: "Cerca TV in rete" }));
    const found = await screen.findByRole("region", { name: "TV trovate" });
    expect(found).toHaveTextContent("UE85DU7190UXZT");
    expect(last("/api/tv/discover")?.body).toEqual({ subnet: "192.168.30.0/24" });
    await user.click(within(found).getByRole("button", { name: /Aggiungi 85/ }));
    await waitFor(() => expect(last("/api/tv/screens")?.body).toEqual({ host: "192.168.30.84", name: "85\" Crystal UHD" }));
  });

  it("a viewer: their own area, no network search, no Fiume, no list of every TV", async () => {
    role = "viewer";
    area = baseArea({ can_discover: false, suggested_subnet: null, screens: baseArea().screens.filter((s) => s.key !== "flow") });
    renderApp("/schermo-tv");
    expect(await screen.findByRole("heading", { name: "Schermo TV" })).toBeInTheDocument();
    expect(await screen.findByRole("radio", { name: "Campagna e portafogli" })).toBeInTheDocument();
    expect(screen.queryByRole("radio", { name: "Fiume dei flussi" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Cerca TV in rete" })).not.toBeInTheDocument();
    expect(screen.queryByText("TV di tutti gli utenti")).not.toBeInTheDocument();
  });

  it("«Collega»: the message about the prompt on the TV, the wait, then «TV collegata»", async () => {
    const user = userEvent.setup();
    area = baseArea({ tvs: [tv()] });
    renderApp("/schermo-tv");
    await user.click(await screen.findByRole("button", { name: "Collega" }));
    const progress = await screen.findByRole("status", { name: "Collegamento in corso" });
    expect(progress).toHaveTextContent("Sulla TV comparirà una richiesta: accettala con il telecomando.");
    expect(within(progress).getByRole("progressbar")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Manda alla TV" })).toBeDisabled();
    // the answer on the TV: the next read of the area
    area = { ...area, tvs: [tv({ paired: true, pairing: { state: "ok", message: null, started_at: null } })] };
    expect(await screen.findByText("TV collegata.", {}, { timeout: 4_000 })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Manda alla TV" })).toBeEnabled();
  });

  it("a refused prompt: says how to try again", async () => {
    area = baseArea({ tvs: [tv({ pairing: { state: "refused", message: null, started_at: null } })] });
    renderApp("/schermo-tv");
    expect(await screen.findByText(/rifiutata sulla TV/)).toBeInTheDocument();
  });

  it("paired: «Manda alla TV» sends the chosen screen, «Spegni lo schermo»", async () => {
    const user = userEvent.setup();
    area = baseArea({ tvs: [tv({ paired: true })], link: { active: true, created_at: "2026-11-28T09:00:00Z", url: LINK } });
    renderApp("/schermo-tv");
    expect(await screen.findByText("· in standby")).toBeInTheDocument();
    await user.click(screen.getByRole("radio", { name: "Salute e avvisi" }));
    await user.click(screen.getByRole("button", { name: "Manda alla TV" }));
    expect(await screen.findByText("Schermata aperta sulla TV")).toBeInTheDocument();
    expect(last("/api/tv/screens/t1/send")?.body).toEqual({ screen: "health", rotation: false, interval_s: 30 });
    await user.click(screen.getByRole("button", { name: "Spegni lo schermo" }));
    expect(await screen.findByText("Schermo spento")).toBeInTheDocument();
  });

  it("the TV did not open the page: the address as text and a QR code", async () => {
    const user = userEvent.setup();
    sendResult = { ok: false, message: "La TV non ha aperto il browser: apri il link a mano", url: LINK, powered_on: false, app_id: null, confirmed: false };
    area = baseArea({ tvs: [tv({ paired: true })] });
    renderApp("/schermo-tv");
    await user.click(await screen.findByRole("button", { name: "Manda alla TV" }));
    expect(await screen.findByText("La TV non ha aperto il browser: apri il link a mano")).toBeInTheDocument();
    const row = screen.getByRole("listitem", { name: "Salotto" });
    expect(within(row).getByTestId("tv-link-url")).toHaveTextContent(LINK);
    expect(await within(row).findByRole("img", { name: "Codice QR del link della TV" })).toHaveAttribute("src", expect.stringMatching(/^data:image\/png/));
  });

  it("screen choice: rotation with its interval, saved without contacting the TV", async () => {
    const user = userEvent.setup();
    renderApp("/schermo-tv");
    await user.click(await screen.findByRole("switch", { name: "Rotazione" }));
    const interval = screen.getByLabelText("Ogni (secondi)");
    await user.clear(interval);
    await user.type(interval, "5");
    expect(screen.getByText("da 15 a 300 secondi")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Cambia sulla TV" })).toBeDisabled();
    await user.clear(interval);
    await user.type(interval, "20");
    await user.click(screen.getByRole("button", { name: "Cambia sulla TV" }));
    expect(await screen.findByText("Scelta salvata.")).toBeInTheDocument();
    expect(last("/api/tv/display")?.body).toEqual({ screen: "campaign", rotation: true, interval_s: 20 });
    expect(calls.some((c) => c.path.endsWith("/send"))).toBe(false);
  });

  it("sounds: off by default; on with volume and quiet hours", async () => {
    const user = userEvent.setup();
    renderApp("/schermo-tv");
    const sw = await screen.findByRole("switch", { name: "Suoni accesi" });
    expect(sw).toHaveAttribute("aria-checked", "false");
    await user.click(sw);
    const start = screen.getByLabelText("Silenzio dalle");
    await user.clear(start);
    expect(screen.getByText(/sia l'inizio sia la fine/)).toBeInTheDocument();
    await user.type(start, "23:00");
    await user.click(screen.getByRole("button", { name: "Salva i suoni" }));
    expect(await screen.findByText("Suoni salvati.")).toBeInTheDocument();
    expect(last("/api/tv/display")?.body).toEqual({ sound_enabled: true, sound_volume: 40, quiet_start: "23:00", quiet_end: "08:00" });
  });

  it("the display link: create it (text and QR), then revoke it", async () => {
    const user = userEvent.setup();
    renderApp("/schermo-tv");
    await user.click(await screen.findByRole("button", { name: "Crea il link" }));
    expect(await screen.findByTestId("tv-link-url")).toHaveTextContent(LINK);
    await user.click(screen.getByRole("button", { name: "Revoca il link" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Revoca" }));
    expect(await screen.findByText(/Nessun link attivo/)).toBeInTheDocument();
    expect(last("/api/tv/link/revoke")).toBeTruthy();
  });

  it("opened from localhost: asks for the portal address the TV can reach", async () => {
    const user = userEvent.setup();
    area = baseArea({ portal_url_detected: null });
    renderApp("/schermo-tv");
    expect(await screen.findByText(/scrivi l'indirizzo di rete del portale/)).toBeInTheDocument();
    await user.type(screen.getByLabelText("Indirizzo del portale visto dalla TV"), "https://192.168.30.7");
    await user.click(screen.getByRole("button", { name: "Salva l'indirizzo" }));
    await waitFor(() => expect(last("/api/tv/display")?.body).toEqual({ portal_url: "https://192.168.30.7" }));
  });

  it("admins: every user's TVs, with revoke", async () => {
    const user = userEvent.setup();
    renderApp("/schermo-tv");
    const table = await screen.findByRole("table", { name: "TV di tutti gli utenti" });
    expect(table).toHaveTextContent("luca@example.com");
    await user.click(within(table).getByRole("button", { name: "Revoca" }));
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent("Revocare «Camera» di luca@example.com?");
    await user.click(within(dialog).getByRole("button", { name: "Revoca" }));
    await waitFor(() => expect(last("/api/tv/admin/screens/t9/revoke")).toBeTruthy());
  });

  it("removing a TV asks first", async () => {
    const user = userEvent.setup();
    area = baseArea({ tvs: [tv()] });
    renderApp("/schermo-tv");
    await user.click(await screen.findByRole("button", { name: "Togli" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Togli" }));
    expect(await screen.findByText(/Nessuna TV: aggiungila/)).toBeInTheDocument();
  });
});
