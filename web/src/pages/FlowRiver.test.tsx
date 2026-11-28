import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { FlowAgents, FlowEvent, FlowHealth, FlowMap, FlowSummary, FlowTools } from "@/api/types";
import FlowRiver from "./FlowRiver";

const hooks = vi.hoisted(() => ({
  useFlowMap: vi.fn(),
  useFlowEvents: vi.fn(),
  useFlowSummary: vi.fn(),
  useFlowTools: vi.fn(),
  useFlowAgents: vi.fn(),
  useFlowHealth: vi.fn(),
  useFlowSources: vi.fn(),
  useFlowReplay: vi.fn(),
  useFlowOrderStory: vi.fn(),
  useFlowMcpStory: vi.fn(),
}));
/** the stand-in renderer of the page under test */
const canvas = vi.hoisted(() => ({ current: null as null | Record<string, unknown> }));
vi.mock("@/api/hooks/flow", () => hooks);

// the renderer needs a real canvas: a stand-in that replays every pushed event at once
vi.mock("@/components/flow/RiverCanvas", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/components/flow/RiverCanvas")>()),
  RiverCanvas: class {
    launch: ((e: FlowEvent) => void) | null = null;
    follow: ((e: FlowEvent | null) => void) | null = null;
    hover: ((h: unknown) => void) | null = null;
    select: ((e: FlowEvent) => void) | null = null;
    nodeClick: ((k: string) => boolean) | null = null;
    pushed: FlowEvent[] = [];
    replayed: FlowEvent[] = [];
    badges: Record<string, string> = {};
    health: Record<string, unknown> = {};
    windowMin = 0;
    replaying: [number, number] | null = null;
    constructor() {
      canvas.current = this as unknown as Record<string, unknown>;
    }
    setMap() {}
    setSummary() {}
    setWindow(m: number) {
      this.windowMin = m;
    }
    setPaused() {}
    setSpeed() {}
    setHealth(h: Record<string, unknown>) {
      this.health = h;
    }
    setBadges(b: Record<string, string>) {
      this.badges = b;
    }
    onSelect(cb: (e: FlowEvent) => void) {
      this.select = cb;
    }
    onNodeClick(cb: (k: string) => boolean) {
      this.nodeClick = cb;
    }
    startReplay(from: number, to: number) {
      this.replaying = [from, to];
      this.replayed = [];
    }
    addReplay(events: FlowEvent[]) {
      this.replayed = events;
    }
    setReplaySpeed() {}
    setReplayHorizon() {}
    seek() {}
    stopReplay() {
      this.replaying = null;
    }
    replayClock() {
      return this.replaying ? this.replaying[0] : null;
    }
    replayEnded() {
      return false;
    }
    linkDirection(): [string, string] | null {
      return ["pc", "mcp"];
    }
    setFollow(on: boolean) {
      const order = this.pushed.find((e) => e.hops[0] === "place_order");
      if (on && order) this.follow?.(order);
    }
    onHover(cb: (h: unknown) => void) {
      this.hover = cb;
    }
    onFollow(cb: (e: FlowEvent | null) => void) {
      this.follow = cb;
    }
    onLaunch(cb: (e: FlowEvent) => void) {
      this.launch = cb;
    }
    push(events: FlowEvent[]) {
      this.pushed.push(...events);
      events.forEach((e) => this.launch?.(e));
    }
    destroy() {}
  },
}));

const MAP: FlowMap = {
  nodes: [
    { key: "agent:a1", label: "Portfolio Manager", kind: "agent", column: 0, description: "Decide" },
    { key: "pc", label: "Paperclip", kind: "agent", column: 1, description: "Orchestratore" },
    { key: "mcp", label: "MCP SMTrading", kind: "core", column: 2, description: "La porta" },
    { key: "ng", label: "Neurograph", kind: "ng", column: 3, description: "Forchetta" },
    { key: "eng", label: "Motore ordini", kind: "core", column: 3, description: "Controlli" },
  ],
  links: [
    ["agent:a1", "pc"],
    ["pc", "mcp"],
    ["mcp", "ng"],
  ],
};

const BUSY: FlowSummary = {
  links: { "pc|mcp": { calls: 25, errors: 1, top_tools: [{ tool: "get_forecast", calls: 25 }] } },
  nodes: { pc: 25, mcp: 25 },
  head: { calls_per_min: 25, orders_today: 3, errors_window: 1, mcp_latency_ms: 240 },
};
const QUIET: FlowSummary = {
  links: {},
  nodes: {},
  head: { calls_per_min: 0, orders_today: 0, errors_window: 0, mcp_latency_ms: null },
};

const EVENTS: FlowEvent[] = [
  {
    id: "mcp:7",
    ts: "2026-11-28T10:00:00Z",
    kind: "mcp",
    who: "agent:a1",
    path: ["agent:a1", "pc", "mcp", "ng", "mcp", "pc"],
    hops: ["get_forecast", "get_forecast", "get_forecast", "risposta", "risposta"],
    outcome: "ok",
    latency_ms: 900,
    symbol: "SPY",
  },
];
const NONE: FlowEvent[] = [];

const AGENTS: FlowAgents = {
  available: true,
  note: null,
  read_at: "2026-11-28T10:00:00Z",
  currency: "USD",
  agents: [
    {
      key: "agent:a1",
      name: "Portfolio Manager",
      paperclip_id: "p1",
      status: "idle",
      status_it: "in attesa",
      today_usd: "1.2",
      month_usd: "12",
      cap_usd: "50",
      runs: [{ id: "r1", ts: "2026-11-28T09:58:00Z", reason: "timer", reason_it: "programmata", outcome: "riuscita", latency_ms: 60_000 }],
    },
  ],
  runs: [],
};
const DOWN: FlowAgents = { available: false, note: "Paperclip non raggiungibile: costi non disponibili", read_at: null, currency: "USD", agents: [{ ...AGENTS.agents[0], today_usd: null, month_usd: null, cap_usd: null, runs: [] }], runs: [] };
const HEALTH: FlowHealth = {
  checked_at: "2026-11-28T10:00:00Z",
  nodes: { ng: { state: "errore", reason: "ultima get_forecast fallita alle 10:58" }, mcp: { state: "ok", reason: "risponde" } },
};
const TOOLS: FlowTools = {
  window: 1,
  families: [{ key: "orders", label: "ordini", tools: [{ tool: "place_order", dest: "eng", calls: 2, errors: 0, avg_ms: 150 }] }],
  other: [],
  links: { "pc|mcp": [{ tool: "get_forecast", calls: 25, errors: 1, avg_ms: 880 }] },
  truncated: false,
};
const IDLE_REPLAY = { events: [], pages: 0, done: false, capped: false, error: null };

function mockHooks({ summary = BUSY, events = EVENTS, stale = false, agents = AGENTS } = {}) {
  hooks.useFlowMap.mockReturnValue({ data: MAP });
  hooks.useFlowSummary.mockReturnValue({ data: summary });
  hooks.useFlowEvents.mockReturnValue({ events, now: "2026-11-28T10:00:01Z", stale });
  hooks.useFlowTools.mockReturnValue({ data: TOOLS });
  hooks.useFlowAgents.mockReturnValue({ data: agents });
  hooks.useFlowHealth.mockReturnValue({ data: HEALTH });
  hooks.useFlowSources.mockReturnValue({ data: undefined });
  hooks.useFlowReplay.mockReturnValue(IDLE_REPLAY);
  hooks.useFlowOrderStory.mockReturnValue({ data: undefined, isError: false });
  hooks.useFlowMcpStory.mockReturnValue({ data: undefined, isError: false });
}
const river = () => canvas.current as unknown as {
  hover: (h: unknown) => void;
  select: (e: FlowEvent) => void;
  nodeClick: (k: string) => boolean;
  badges: Record<string, string>;
  health: Record<string, unknown>;
  replayed: FlowEvent[];
  replaying: [number, number] | null;
  pushed: FlowEvent[];
  windowMin: number;
};

beforeEach(() => {
  vi.clearAllMocks();
});

describe("FlowRiver", () => {
  it("shows the title and the counters of the summary", () => {
    mockHooks();
    render(<FlowRiver />);
    expect(screen.getByRole("heading", { name: "Fiume dei flussi" })).toBeInTheDocument();
    const counters = screen.getByRole("group", { name: "Contatori" });
    expect(within(counters).getByText("25")).toBeInTheDocument();
    expect(within(counters).getByText("3")).toBeInTheDocument();
    expect(within(counters).getByText("1")).toBeInTheDocument();
    expect(within(counters).getByText("240 ms")).toBeInTheDocument();
    expect(screen.getByRole("img", { name: /25 chiamate al minuto/ })).toBeInTheDocument();
    expect(screen.queryByText(/Nessuna chiamata/)).not.toBeInTheDocument();
  });

  it("writes the replayed calls in the live diary", () => {
    mockHooks();
    render(<FlowRiver />);
    const diary = screen.getByRole("log", { name: "Chiamate in diretta" });
    expect(within(diary).getByText("Portfolio Manager")).toBeInTheDocument();
    expect(within(diary).getByText("get_forecast")).toBeInTheDocument();
    expect(diary).toHaveTextContent(/\d\d:\d\d:\d\d Portfolio Manager get_forecast · 900 ms/);
  });

  it("says when there was no call in the window", () => {
    mockHooks({ summary: QUIET, events: NONE });
    render(<FlowRiver />);
    expect(screen.getByRole("status")).toHaveTextContent("Nessuna chiamata negli ultimi 1 minuti");
    expect(screen.getByText("—")).toBeInTheDocument(); // no mean time
  });

  it("warns when the events can not be read, keeping the river", () => {
    mockHooks({ stale: true });
    render(<FlowRiver />);
    expect(screen.getByRole("status")).toHaveTextContent("dati non aggiornati, nuovo tentativo…");
    expect(screen.getByRole("img", { name: /25 chiamate al minuto/ })).toBeInTheDocument();
  });

  it("asks the summary of 5 minutes when the width is set on 5 min", async () => {
    mockHooks();
    render(<FlowRiver />);
    expect(hooks.useFlowSummary).toHaveBeenLastCalledWith(1);
    await userEvent.click(screen.getByRole("button", { name: "5 min" }));
    expect(hooks.useFlowSummary).toHaveBeenLastCalledWith(5);
    expect(screen.getByRole("button", { name: "5 min" })).toHaveAttribute("aria-pressed", "true");
  });

  it("explains the followed order with the names of the map", async () => {
    const order: FlowEvent = {
      ...EVENTS[0],
      id: "mcp:8",
      path: ["agent:a1", "pc", "mcp", "eng"],
      hops: ["place_order", "place_order", "place_order"],
    };
    mockHooks({ events: [order] });
    render(<FlowRiver />);
    await userEvent.click(screen.getByRole("button", { name: "◎ Segui un ordine" }));
    const story = screen.getByText(/chiede a SMTrading/).closest("div")!;
    expect(story).toHaveTextContent("Portfolio Manager");
    expect(story).toHaveTextContent("Paperclip");
    expect(story).toHaveTextContent("MCP SMTrading");
    expect(story).toHaveTextContent("Motore ordini");
    expect(story).toHaveTextContent("SPY");
  });

  it("keeps the general view as v1 when nothing is opened", () => {
    mockHooks();
    render(<FlowRiver />);
    expect(screen.queryByRole("region", { name: "Moviola" })).not.toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "⏸ Pausa" })).toBeInTheDocument();
  });

  it("widens the bands on the whole day with «oggi»", async () => {
    mockHooks();
    render(<FlowRiver />);
    await userEvent.click(within(screen.getByRole("group", { name: "Larghezza su" })).getByRole("button", { name: "oggi" }));
    expect(hooks.useFlowSummary).toHaveBeenLastCalledWith("today");
    expect(screen.getByRole("group", { name: "Contatori" })).toHaveTextContent("errori oggi");
    expect(river().windowMin).toBeGreaterThanOrEqual(1);
  });

  it("opens the replay bar, loads a preset and goes back to live", async () => {
    const replayed = [{ ...EVENTS[0], id: "mcp:100", ts: "2026-11-28T09:30:00Z" }];
    mockHooks();
    render(<FlowRiver />);
    await userEvent.click(screen.getByRole("button", { name: "⏮ Moviola" }));
    expect(screen.getByRole("region", { name: "Moviola" })).toHaveTextContent("moviola · scegli un intervallo");
    hooks.useFlowReplay.mockReturnValue({ events: replayed, pages: 1, done: true, capped: false, error: null });
    await userEvent.click(screen.getByRole("button", { name: "ultima ora" }));
    const [range] = hooks.useFlowReplay.mock.lastCall!;
    expect(range.to - range.from).toBe(3_600_000);
    expect(river().replaying).toEqual([range.from, range.to]);
    expect(river().replayed.map((e) => e.id)).toEqual(["mcp:100"]);
    expect(screen.getByRole("region", { name: "Moviola" })).toHaveTextContent("moviola · ora mostrata");
    expect(screen.getByRole("log", { name: "Chiamate in moviola" })).toBeInTheDocument();
    expect(screen.getByRole("group", { name: "Contatori" })).toHaveTextContent("errori nell'intervallo");
    await userEvent.click(screen.getByRole("button", { name: "Torna al vivo" }));
    expect(river().replaying).toBeNull();
    expect(screen.queryByRole("region", { name: "Moviola" })).not.toBeInTheDocument();
    expect(hooks.useFlowReplay).toHaveBeenLastCalledWith(null);
  });

  it("opens an order's story from the diary and from a comet", async () => {
    const sent: FlowEvent = {
      id: "order:9b1c6c2e-1111-4111-8111-111111111111:sent",
      ts: "2026-11-28T10:00:00Z",
      kind: "order_sent",
      who: "eng",
      path: ["eng", "mcp"],
      hops: ["invio", "ordine AAPL"],
      outcome: "ok",
      latency_ms: null,
      symbol: "AAPL",
    };
    mockHooks({ events: [sent] });
    render(<FlowRiver />);
    await userEvent.click(screen.getByRole("button", { name: /storia/ }));
    expect(screen.getByRole("dialog", { name: "Storia dell'ordine" })).toBeInTheDocument();
    expect(hooks.useFlowOrderStory).toHaveBeenLastCalledWith("9b1c6c2e-1111-4111-8111-111111111111");
    await userEvent.click(screen.getByRole("button", { name: "Chiudi la storia" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    act(() => river().select({ ...EVENTS[0], id: "mcp:77", hops: ["place_order", "place_order", "place_order"] }));
    expect(hooks.useFlowMcpStory).toHaveBeenLastCalledWith("77");
  });

  it("rings the nodes with their health and explains it in the card", () => {
    mockHooks();
    render(<FlowRiver />);
    expect(river().health).toEqual(HEALTH.nodes);
    act(() => river().hover({ node: "ng", x: 10, y: 10 }));
    expect(screen.getByText("Neurograph").closest("div[class*=absolute]")).toHaveTextContent("errore · ultima get_forecast fallita alle 10:58");
  });

  it("badges the agents with today's and the month's spend and the cap, and lists the last runs", () => {
    mockHooks();
    render(<FlowRiver />);
    expect(river().badges).toEqual({ "agent:a1": "oggi 1,20 $ · mese 12,00 $ / 50,00 $" });
    act(() => river().hover({ node: "agent:a1", x: 10, y: 10 }));
    const card = screen.getAllByText("Portfolio Manager").map((e) => e.closest("div[class*=absolute]")).find(Boolean)!;
    expect(card).toHaveTextContent("spesa oggi1,20 $");
    expect(card).toHaveTextContent("tetto mensile50,00 $");
    expect(card).toHaveTextContent("programmata · riuscita");
  });

  it("says when Paperclip is down: costs not available, the river keeps working", () => {
    mockHooks({ agents: DOWN });
    render(<FlowRiver />);
    expect(screen.getByText("Paperclip non raggiungibile: costi non disponibili")).toBeInTheDocument();
    expect(river().badges).toEqual({ "agent:a1": "costi non disponibili" });
    expect(screen.getByRole("img", { name: /25 chiamate al minuto/ })).toBeInTheDocument();
  });

  it("merges the recent Paperclip runs into the live river", () => {
    const run = { ...EVENTS[0], id: "run:r9", ts: new Date(Date.now() - 60_000).toISOString(), kind: "run", who: "pc", path: ["pc", "agent:a1"], hops: ["programmata"], reason: "timer", run_outcome: "riuscita" };
    const old = { ...run, id: "run:r1", ts: new Date(Date.now() - 3_600_000).toISOString() };
    mockHooks({ agents: { ...AGENTS, runs: [old, run] } });
    render(<FlowRiver />);
    expect(river().pushed.map((e) => e.id)).toContain("run:r9");
    expect(river().pushed.map((e) => e.id)).not.toContain("run:r1");
  });

  it("opens the lens on MCP SMTrading and closes it with a click outside", async () => {
    mockHooks();
    render(<FlowRiver />);
    let taken = false;
    act(() => {
      taken = river().nodeClick("mcp");
    });
    expect(taken).toBe(true);
    expect(river().nodeClick("ng")).toBe(false);
    const lens = screen.getByRole("dialog", { name: "Lente su MCP SMTrading" });
    expect(within(lens).getByRole("listitem", { name: "ordini" })).toHaveTextContent("place_order");
    expect(hooks.useFlowTools).toHaveBeenLastCalledWith(1, true);
    await userEvent.click(screen.getByTestId("lens-backdrop"));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("lists every tool of a link with its mean time in the link card", () => {
    mockHooks();
    render(<FlowRiver />);
    act(() => river().hover({ link: "pc|mcp", x: 10, y: 10 }));
    expect(screen.getByText(/Paperclip ↔ MCP SMTrading/).closest("div[class*=absolute]")).toHaveTextContent("get_forecast25 · 880 ms · 1 ✕");
  });
});
