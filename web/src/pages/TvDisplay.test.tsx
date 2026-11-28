import { act, screen, within } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import type { TvCampaignData, TvFlowData, TvHealthData, TvSelection, TvSoundEvent } from "@/api/tvTypes";
import { renderApp } from "@/test/render";
import { tvEnv } from "@/tv/display";

// the sounds: a stand-in recording what the page hands over
const audio = vi.hoisted(() => ({
  calls: [] as { op: string; arg: unknown }[],
  blocked: false,
}));
vi.mock("@/tv/sounds", () => ({
  TvSounds: class {
    configure(s: unknown) {
      audio.calls.push({ op: "configure", arg: s });
    }
    baseline(e: unknown) {
      audio.calls.push({ op: "baseline", arg: e });
    }
    handle(e: unknown) {
      audio.calls.push({ op: "handle", arg: e });
    }
    blocked() {
      return audio.blocked;
    }
    onChange() {
      return () => {};
    }
    unlock() {
      audio.calls.push({ op: "unlock", arg: null });
    }
    destroy() {}
  },
}));

// the river needs a real canvas: a stand-in that records what it gets
const river = vi.hoisted(() => ({ maps: 0, pushed: [] as string[], summary: null as unknown }));
vi.mock("@/components/flow/RiverCanvas", async (orig) => ({
  ...(await orig<typeof import("@/components/flow/RiverCanvas")>()),
  RiverCanvas: class {
    launch: ((e: unknown) => void) | null = null;
    setMap() {
      river.maps++;
    }
    setSummary(s: unknown) {
      river.summary = s;
    }
    setWindow() {}
    setHealth() {}
    onLaunch(cb: (e: unknown) => void) {
      this.launch = cb;
    }
    push(events: { id: string }[]) {
      events.forEach((e) => {
        river.pushed.push(e.id);
        this.launch?.(e);
      });
    }
    destroy() {}
  },
}));

const SOUND = { enabled: true, volume: 40, quiet_start: "22:00", quiet_end: "08:00", tz: "Europe/Rome", quiet_now: false };
const sel = (over: Partial<TvSelection> = {}): TvSelection => ({
  screen: "campaign",
  rotation: false,
  interval_s: 30,
  version: 1,
  updated_at: "2026-11-28T10:00:00Z",
  screens: ["flow", "campaign", "health"],
  poll_s: 4,
  now: "2026-11-28T10:00:00Z",
  sound: SOUND,
  sound_events: [],
  sound_cursor: "2026-11-28T10:00:00Z",
  ...over,
});

const CAMPAIGN: TvCampaignData = {
  screen: "campaign",
  now: "2026-11-28T10:00:00Z",
  campaign: { name: "Campagna di prova", start: "2026-11-23", end: "2026-12-23", day: 6, total_days: 30, state: "in_corso" },
  equity: { current_eur: "31250.40", as_of: "2026-11-28T10:00:00Z", portfolios: 3 },
  goal: {
    label: "+5% in 30 giorni", name: "Obiettivo", status: "in corso", start_eur: "30000", current_eur: "31250.40", target_eur: "31500",
    pct: "4.17", target_pct: "5", period_start: "2026-11-23", period_end: "2026-12-23", as_of: "2026-11-28T10:00:00Z",
  },
  portfolios: [{ id: "p1", name: "Momentum", code: "MOM", equity_eur: "10400", day_change_eur: "-25.5", day_change_pct: "-0.24", as_of: "2026-11-28T10:00:00Z" }],
  positions: [
    { portfolio: "Momentum", symbol: "AAPL", qty: "3", avg_price: "190.5", opened_at: "2026-11-24T15:00:00Z", stop_price: "180", stop_type: "stop", trail_pct: null },
    { portfolio: "Momentum", symbol: "MSFT", qty: "1", avg_price: "410", opened_at: "2026-11-24T15:00:00Z", stop_price: null, stop_type: null, trail_pct: null },
  ],
  fills: [{ portfolio: "Momentum", symbol: "AAPL", side: "buy", qty: "3", price: "190.5", at: "2026-11-28T09:45:00Z" }],
  notes: [],
};

const HEALTH: TvHealthData = {
  screen: "health",
  now: "2026-11-28T10:00:00Z",
  status: "attenzione",
  checked_at: "2026-11-28T09:59:00Z",
  sections: [
    { key: "services", label: "Servizi", status: "ok", summary: "Tutti i servizi rispondono" },
    { key: "quotes", label: "Quotazioni", status: "attenzione", summary: "2 quotazioni vecchie" },
  ],
  nodes: [{ key: "alpaca", label: "Alpaca", kind: "broker", state: "errore", reason: "non risponde" }],
  alerts: { on: true, open: [{ title: "Broker non raggiungibile", label: "Broker", level: "problema", since: "2026-11-28T09:40:00Z" }], recent: [{ label: "Backup", at: "2026-11-28T08:00:00Z" }] },
};

const FLOW: TvFlowData = {
  screen: "flow",
  now: "2026-11-28T10:00:00Z",
  map: { nodes: [{ key: "pm", label: "Portfolio Manager", kind: "agent", column: 0, description: "" }], links: [] },
  summary: { links: {}, nodes: {}, head: { calls_per_min: 12.5, orders_today: 7, errors_window: 1, mcp_latency_ms: 140 } },
  health: { checked_at: "2026-11-28T10:00:00Z", nodes: {} },
  events: [{ id: "e1", ts: "2026-11-28T09:59:58Z", kind: "mcp", who: "pm", path: ["pm"], hops: ["get_quote"], outcome: "ok", latency_ms: 80, symbol: null }],
  cursor: "2026-11-28T10:00:00Z",
};

let selection: TvSelection = sel();
let selectionStatus = 200;
const seen: { method: string; url: string; body?: unknown }[] = [];
const server = setupServer(
  http.post("*/api/tv-display/session", async ({ request }) => {
    const body = await request.json();
    seen.push({ method: "POST", url: request.url, body });
    return (body as { token: string }).token === "good" ? HttpResponse.json({ ok: true }) : HttpResponse.json({ code: "FORBIDDEN", message: "link non valido" }, { status: 401 });
  }),
  http.get("*/api/tv-display/selection", ({ request }) => {
    seen.push({ method: "GET", url: request.url });
    if (selectionStatus === 0) return HttpResponse.error();
    if (selectionStatus !== 200) return HttpResponse.json({ code: "FORBIDDEN", message: "revocato" }, { status: selectionStatus });
    const s = selection;
    selection = { ...selection, sound_events: [] };
    return HttpResponse.json(s);
  }),
  http.get("*/api/tv-display/campaign", () => HttpResponse.json(CAMPAIGN)),
  http.get("*/api/tv-display/health", () => HttpResponse.json(HEALTH)),
  http.get("*/api/tv-display/flow", ({ request }) => {
    seen.push({ method: "GET", url: request.url });
    return HttpResponse.json(FLOW);
  }),
);
beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
beforeEach(() => {
  selection = sel();
  selectionStatus = 200;
  seen.length = 0;
  audio.calls = [];
  audio.blocked = false;
  river.maps = 0;
  river.pushed = [];
});
afterEach(() => {
  server.resetHandlers();
  vi.useRealTimers();
});
afterAll(() => server.close());

/** advances the fake clock and lets the requests settle */
async function tick(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

describe("TvDisplay", () => {
  it("trades the link in the fragment for the cookie, clears it, then reads without the token", async () => {
    const { router } = renderApp("/tv#token=good");
    expect(await screen.findByRole("heading", { name: "Campagna e portafogli" })).toBeInTheDocument();
    expect(seen[0]).toMatchObject({ method: "POST", body: { token: "good" } });
    expect(router.state.location.hash).toBe("");
    const reads = seen.filter((r) => r.method === "GET");
    expect(reads.length).toBeGreaterThan(0);
    for (const r of reads) expect(r.url).not.toContain("token");
    // no portal chrome
    expect(screen.queryByRole("navigation")).not.toBeInTheDocument();
  });

  it("the campaign screen: day, total, goal, portfolios, positions with stop, fills", async () => {
    renderApp("/tv#token=good");
    expect(await screen.findByText("Campagna di prova")).toBeInTheDocument();
    expect(screen.getByText("Giorno 6 di 30")).toBeInTheDocument();
    expect(screen.getByText("31.250,40 €")).toBeInTheDocument();
    expect(screen.getByLabelText("Andamento verso l'obiettivo")).toHaveTextContent("+4,17% su +5,00%");
    const pf = screen.getByRole("region", { name: "Portafogli oggi" });
    expect(pf).toHaveTextContent("Momentum");
    expect(pf).toHaveTextContent("-25,50 €");
    const pos = screen.getByRole("region", { name: "Posizioni aperte" });
    expect(within(pos).getByText("stop 180,00")).toBeInTheDocument();
    expect(within(pos).getByText("senza stop")).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Ultimi ordini eseguiti" })).toHaveTextContent("acquisto AAPL");
  });

  it("the campaign screen with nothing to show", async () => {
    server.use(
      http.get("*/api/tv-display/campaign", () =>
        HttpResponse.json({ ...CAMPAIGN, campaign: null, equity: null, goal: null, portfolios: [], positions: [], fills: [], notes: ["nessun dato"] }),
      ),
    );
    renderApp("/tv#token=good");
    expect(await screen.findByText("Nessuna campagna in corso")).toBeInTheDocument();
    expect(screen.getByText("Nessuna posizione aperta")).toBeInTheDocument();
    expect(screen.getByText("Nessun ordine eseguito")).toBeInTheDocument();
    expect(screen.getByText("nessun dato")).toBeInTheDocument();
  });

  it("a screen whose data does not arrive says so and keeps trying", async () => {
    server.use(http.get("*/api/tv-display/campaign", () => new HttpResponse(null, { status: 503 })));
    renderApp("/tv#token=good");
    expect(await screen.findByText("Dati non disponibili, nuovo tentativo…")).toBeInTheDocument();
  });

  it("the health screen (with the cookie only, no fragment): status, sections, nodes, alert titles", async () => {
    selection = sel({ screen: "health" });
    renderApp("/tv");
    expect(await screen.findByRole("heading", { name: "Salute e avvisi" })).toBeInTheDocument();
    expect(seen.some((r) => r.method === "POST")).toBe(false);
    expect(await screen.findByRole("status", { name: "Stato complessivo" })).toHaveTextContent("Attenzione");
    expect(screen.getByLabelText("Quotazioni")).toHaveTextContent("2 quotazioni vecchie");
    expect(screen.getByRole("region", { name: "Flussi" })).toHaveTextContent("Alpaca");
    expect(screen.getByRole("region", { name: "Avvisi aperti" })).toHaveTextContent("Broker non raggiungibile");
    expect(screen.getByRole("region", { name: "Ultimi avvisi" })).toHaveTextContent("Backup");
  });

  it("the flow screen: the river gets map and events, large counters and diary", async () => {
    selection = sel({ screen: "flow" });
    renderApp("/tv");
    expect(await screen.findByRole("heading", { name: "Fiume dei flussi" })).toBeInTheDocument();
    const counters = await screen.findByRole("group", { name: "Contatori" });
    expect(await within(counters).findByText("12,5")).toBeInTheDocument();
    expect(within(counters).getByText("140 ms")).toBeInTheDocument();
    expect(river.maps).toBe(1);
    expect(river.pushed).toContain("e1");
    expect(screen.getByRole("log", { name: "Chiamate in diretta" })).toHaveTextContent("Portfolio Manager get_quote");
  });

  it("a link refused at the start: the revoked message", async () => {
    renderApp("/tv#token=bad");
    expect(await screen.findByRole("alert")).toHaveTextContent("Collegamento revocato: apri di nuovo il link dal portale");
  });

  it("a refused link is cleared from the address bar too", async () => {
    const { router } = renderApp("/tv#token=bad");
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(router.state.location.hash).toBe("");
  });

  it("a new link in the fragment (rotated) opens the session again without a reload", async () => {
    const { router } = renderApp("/tv#token=bad");
    expect(await screen.findByRole("alert")).toHaveTextContent("Collegamento revocato");
    await act(async () => {
      await router.navigate("/tv#token=good");
    });
    expect(await screen.findByRole("heading", { name: "Campagna e portafogli" })).toBeInTheDocument();
    expect(seen.filter((r) => r.method === "POST").map((r) => r.body)).toEqual([{ token: "bad" }, { token: "good" }]);
    expect(router.state.location.hash).toBe("");
  });

  it("a plain `hashchange` with a new link is followed as well", async () => {
    renderApp("/tv#token=bad");
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    act(() => {
      window.location.hash = "#token=good";
    });
    expect(await screen.findByRole("heading", { name: "Campagna e portafogli" })).toBeInTheDocument();
    expect(seen.filter((r) => r.method === "POST").at(-1)?.body).toEqual({ token: "good" });
    window.location.hash = "";
  });

  it("a link revoked while shown: the revoked message, the screen goes away", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    renderApp("/tv");
    expect(await screen.findByRole("heading", { name: "Campagna e portafogli" })).toBeInTheDocument();
    selectionStatus = 401;
    await tick(5_000);
    expect(await screen.findByRole("alert")).toHaveTextContent("Collegamento revocato");
    expect(screen.queryByRole("heading", { name: "Campagna e portafogli" })).not.toBeInTheDocument();
  });

  it("follows the selection made in the portal on the next read (no reopening)", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    renderApp("/tv");
    expect(await screen.findByRole("heading", { name: "Campagna e portafogli" })).toBeInTheDocument();
    selection = sel({ screen: "health", version: 2 });
    await tick(4_000);
    expect(await screen.findByRole("heading", { name: "Salute e avvisi" })).toBeInTheDocument();
    // every read after the first one asks for what is new since the previous one
    const reads = seen.filter((r) => r.url.includes("/selection"));
    expect(reads[0].url).not.toContain("after=");
    expect(reads[1].url).toContain("after=");
  });

  it("rotation: the screens in turn, starting from the chosen one", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    selection = sel({ screen: "health", rotation: true, interval_s: 15, screens: ["campaign", "health"] });
    renderApp("/tv");
    expect(await screen.findByRole("heading", { name: "Salute e avvisi" })).toBeInTheDocument();
    expect(screen.getByText("rotazione 1 di 2")).toBeInTheDocument();
    await tick(15_000);
    expect(await screen.findByRole("heading", { name: "Campagna e portafogli" })).toBeInTheDocument();
    await tick(15_000);
    expect(await screen.findByRole("heading", { name: "Salute e avvisi" })).toBeInTheDocument();
  });

  it("an api restart: keeps the screen, says it, and recovers", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    renderApp("/tv");
    expect(await screen.findByRole("heading", { name: "Campagna e portafogli" })).toBeInTheDocument();
    selectionStatus = 0;
    await tick(4_000);
    expect(await screen.findByText("collegamento al server perso, nuovo tentativo…")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Campagna e portafogli" })).toBeInTheDocument();
    selectionStatus = 200;
    await tick(5_000);
    await vi.waitFor(() => expect(screen.queryByText("collegamento al server perso, nuovo tentativo…")).not.toBeInTheDocument());
  });

  it("sounds: the first read is a baseline, later events are handed over; the blocked hint", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    audio.blocked = true;
    const fill: TvSoundEvent = { id: "fill:1", kind: "fill_buy", side: "buy", symbol: "AAPL", at: "2026-11-28T10:00:01Z" };
    renderApp("/tv");
    expect(await screen.findByText("premi OK sul telecomando per attivare i suoni")).toBeInTheDocument();
    expect(audio.calls.find((c) => c.op === "baseline")).toBeTruthy();
    expect(audio.calls.find((c) => c.op === "configure")?.arg).toMatchObject({ enabled: true, volume: 40 });
    selection = sel({ sound_events: [fill] });
    await tick(4_000);
    await vi.waitFor(() => expect(audio.calls.filter((c) => c.op === "handle").map((c) => c.arg)).toContainEqual([fill]));
    // the remote's OK unlocks the audio
    act(() => {
      window.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter" }));
    });
    expect(audio.calls.some((c) => c.op === "unlock")).toBe(true);
  });

  it("a new build on the server: the page reloads itself after a good read", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const reload = vi.fn();
    const saved = { ...tvEnv };
    tvEnv.reload = reload;
    tvEnv.ownBuild = "old";
    server.use(
      http.get("*/api/tv-display/selection", () => HttpResponse.json(sel(), { headers: { "X-SMT-Build": "new" } })),
    );
    try {
      renderApp("/tv");
      await tick(4_000);
      await vi.waitFor(() => expect(reload).toHaveBeenCalled());
    } finally {
      Object.assign(tvEnv, saved);
    }
  });
});
